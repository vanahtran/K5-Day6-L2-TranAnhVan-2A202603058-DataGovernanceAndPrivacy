# Group Report — Day 06 Data Lab (Field-to-Release PoC)

> Báo cáo hoàn chỉnh bài lab Data Governance & Privacy - Track T04 (Sensor Sync Guard).
> Mọi số liệu được trích xuất trực tiếp từ nhật ký thực thi của hệ thống (`out/poc/run_info.json`, `out/poc/pairs.csv`, `out/poc/decision.json`).

---

## 00 · Header

| Mục | Giá trị |
|---|---|
| Group ID | **G07** |
| Challenge | **T04 · Sensor Sync Guard: Đồng bộ Camera và IMU** |
| Thành viên & Vai trò | **Tran Anh Van** (Lead / Core Algorithm / QA & Review) |
| Git URL | `https://github.com/vanahtran/K5-Day6-L2-TranAnhVan-2A202603058-DataGovernanceAndPrivacy` |
| Tag `demo-180m` (commit) | `b0ce8bf` (hoặc tag `demo-180m`) |
| Tag `final-24h` (commit) | `final-24h` |
| Trạng thái Contract | **PASS** (`status=0`, runtime 0.198s, `pytest -q`: 0 failed) |

---

## 01 · Problem statement

- **Ai bị ảnh hưởng / chịu hậu quả khi hệ thống sai:**
  - Hệ thống định vị xe tự hành (Visual-Inertial Odometry - VIO, SLAM) và các mô hình phát hiện vật thể đa cảm biến 3D (Camera + IMU).
  - Nếu thời gian giữa Camera và IMU bị lệch vài chục mili-giây, xe đang chạy ở vận tốc 60 km/h sẽ ước lượng sai vị trí vật cản gần 1 mét, dẫn đến phanh khẩn cấp trễ hoặc đánh lái sai gây nguy hiểm tính mạng.
  - Nếu cảm biến IMU bị mất tín hiệu (dropout) mà hệ thống vẫn tự động "nối bừa" một mẫu IMU ở xa, hệ thống xe sẽ nhận dữ liệu gia tốc sai lệch và phản ứng sai.
- **Điểm nghẽn cụ thể:**
  - Camera chụp ở tần số ~30 Hz (chu kỳ ~33.3 ms), lưu mốc thời gian dạng số thực (giây).
  - IMU đo ở tần số 100 Hz (chu kỳ 10 ms), lưu mốc thời gian dạng số nguyên (mili-giây).
  - Hai thiết bị chạy trên hai tinh thể thạch anh độc lập, bị lệch pha thời gian (Clock Offset dao động từ -140 ms đến +20 ms giữa các đoạn 20 giây) và có khoảng mất mạng cảm biến kéo dài 1.51 giây.
- **Đầu vào → Đầu ra:**
  - Đầu vào: `camera.csv` (3570 khung hình) và `imu.csv` (11850 mẫu đo gia tốc và vận tốc góc).
  - Đầu ra: `pairs.csv` gồm đúng 2 cột `frame_id, imu_t_ms`. Nếu frame rơi vào vùng mất cảm biến thì để trống (`NaN` = HOLD).
- **Tiêu chí GO / HOLD / REWORK:**
  - **GO:** Tỷ lệ đồng bộ thành công $\ge 95\%$ trên tổng số frame và 100% frame trong vùng dropout được giữ lại an toàn (`NaN`).
  - **HOLD:** Tỷ lệ mất mạng cảm biến $> 5\%$ hoặc không tìm được độ tương quan chéo giữa hai luồng tín hiệu.
  - **REWORK:** Dữ liệu bị lỗi hash manifest hoặc sai lệch đơn vị giây/mili-giây.

---

## 02 · Data & provenance

| Split | N (Frames) | Nguồn / Fixture | Đơn vị thời gian | Dropout / Sự cố |
|---|---|---|---|---|
| **dev (Segments 0, 2, 4)** | 1785 frames | `fixtures/t04_sensor_sync/` | Camera: giây / IMU: ms | Đoạn 2 có IMU gap 1.51s (tại t=45s) |
| **eval (Segments 1, 3, 5)** | 1785 frames | `fixtures/t04_sensor_sync/` | Camera: giây / IMU: ms | Drift và offset lớn (-140ms) |

- **Schema & Units:**
  - `camera.csv`: `frame_id` (str), `t_cam_s` (float, giây), `exposure_ms` (float, ms), `motion_score` (float).
  - `imu.csv`: `t_imu_ms` (int, ms), `ax, ay, az` (float, m/s²), `gx, gy, gz` (float, rad/s).
- **Seed:** 42. Chia split theo segment 20 giây (Segment 0, 2, 4 là dev; Segment 1, 3, 5 là eval theo `segments.csv`).
- **`manifest.csv` verified (sha256):** **PASS** (100% khớp mã hash, kiểm tra bởi `make preflight` và hàm `verify_manifest`).

---

## 03 · Research & design

- **Kỹ thuật lựa chọn (Cross-Correlation on Motion Spikes):**
  - **Nguyên lý vật lý:** Khi xe đi qua gờ giảm tốc hoặc ổ gà, camera bị rung lắc làm `motion_score` tăng vọt, đồng thời cảm biến IMU ghi nhận gia tốc thẳng đứng $|a_z - 9.81|$ tăng vọt tại cùng một thời điểm thực tế.
  - Sử dụng hàm `scipy.signal.correlate` để tìm độ trễ $\Delta t$ tối ưu giữa hai tín hiệu trên cửa sổ trượt từng đoạn 20 giây.
- **Sơ đồ Data Flow:**
  $$\text{camera.csv, imu.csv} \xrightarrow{\text{load}} \text{Chuyển } t_{cam} \times 1000 \xrightarrow{\text{Cross-Corr}} \text{Tìm Offset } \Delta t \xrightarrow{\text{Bù giờ}} t'_{cam} = t_{cam} + \Delta t$$
  $$\xrightarrow{\text{Nearest Match}} \text{Tìm } t_{imu} \text{ gần nhất} \xrightarrow{\text{Dropout Filter}} \text{Nếu khoảng cách } > 50\text{ms} \rightarrow \text{HOLD (NaN)} \xrightarrow{\text{rank \& review}} \text{Decide GO}$$
- **Trade-offs đã cân nhắc:**
  - Chia cửa sổ 20s (segment-based) thay vì tìm 1 offset cố định toàn cục giúp xử lý được hiện tượng trôi nhiệt đồng hồ (clock drift) và nhảy mốc thời gian giữa các đoạn.
  - Sử dụng tìm kiếm nhị phân (`np.searchsorted`) giúp tốc độ khớp 3570 frames với 11850 mẫu chỉ mất dưới 0.2 giây trên CPU thông thường.

---

## 04 · Baseline vs PoC

- **Baseline (Nearest Join ngây thơ):**
  - Ghép mốc thời gian gần nhất mà không bù độ lệch đồng hồ (`offset = 0`).
  - Do Camera bị trễ 40–140 ms so với IMU, Baseline bị ghép sai lệch sự kiện và vượt ngưỡng dung sai 50ms.
- **PoC (Cross-Correlation Sync + Dropout Guard):**
  - Tự động bù offset chính xác theo từng đoạn 20 giây:
    - Segment 0: $-40.0$ ms
    - Segment 1: $-40.0$ ms
    - Segment 2: $-80.0$ ms (kèm xử lý 46 frame mất tín hiệu)
    - Segment 3: $-100.0$ ms
    - Segment 4: $+20.0$ ms
    - Segment 5: $-140.0$ ms
  - Áp dụng bộ lọc dung sai `tol_ms = 50.0 ms`.

### Bảng 1 · So sánh Baseline vs PoC

| Chỉ số đánh giá | Baseline (Ngây thơ) | PoC (Nhóm đề xuất) | Chênh lệch & Nhận xét |
|---|---|---|---|
| **Primary: Pair Accuracy@50ms** | $\approx 25.4\%$ | **$98.7\%$** | **$+73.3\%$** (Vượt trội hoàn toàn) |
| **Median \|time residual\|** | $\approx 40.0$ ms | **$2.45$ ms** | Giảm sai số thời gian xuống mức cực tiểu |
| **Số frame đồng bộ hợp lệ** | 1020 / 3570 | **3524 / 3570** | Khớp khít từng khung hình |
| **Số frame phát hiện Dropout (HOLD)** | 0 frame | **46 frames** | Nhận diện chính xác 100% vùng rớt mạng 1.5s |
| **Thời gian chạy (Runtime CPU)** | 0.07s | **0.19s** | Cực kỳ nhẹ, hoàn toàn offline |

---

## 05 · Evaluation protocol

- **Công thức tính Metric chính (Pair Accuracy@50ms):**
  $$\text{Pair Accuracy@50ms} = \frac{\sum [|t_{pred} - t_{gt}| \le 50\text{ ms}]}{N_{frames\_có\_GT}}$$
  *(Các frame bị rớt mạng cảm biến nếu để trống NaN sẽ được tính là bảo vệ thành công)*.
- **Lệnh chạy chuẩn hợp đồng:**
  ```bash
  make run TRACK=t04
  # hoặc: python -m src.main --config configs/lab.yaml --track t04 --output out/poc --run-name poc --offline --seed 42
  ```

---

## 06 · Result slices & Phân tích lỗi (Failure Modes)

- **Phân tích theo từng lát cắt (Slice):**
  - Các đoạn bình thường (Segment 0, 1, 3, 5): Tương quan chéo đạt đỉnh $r > 0.92$, sai số ghép cặp trung vị chỉ dao động từ $1.67$ đến $5.0$ ms.
- **Failure Case 1 (Sensor Dropout tại Segment 2):**
  - Mẫu IMU từ index 4499 đến 4500 bị hẫng từ `1700000044990` đến `1700000046500` (mất tín hiệu $1510$ ms).
  - Có 46 frame camera (`f01352` đến `f01397`) rơi vào khoảng hẫng này.
  - **Cách xử lý của PoC:** Phát hiện khoảng cách tới mẫu IMU gần nhất $> 50$ ms $\rightarrow$ Gán `NaN` vào cột `imu_t_ms`.
- **Failure Case 2 (Clock Reset / Shock):**
  - Trong kịch bản shock test (`fixtures/t04_sensor_sync/shock/`), camera clock bị reset đột ngột giữa chừng.
  - Thuật toán phân đoạn 20s của PoC cô lập được đoạn bị reset mà không làm hỏng các đoạn trước đó.

---

## 07 · Human & decision

- **Hàng đợi kiểm duyệt (Human-in-the-loop):**
  - Hệ thống tự động trích xuất Top-20 frame có điểm bất thường cao nhất vào file `out/poc/review_queue.csv`.
  - 100% các mẫu trong Top-20 là các frame rơi vào vùng mất cảm biến `imu_dropout_or_gap`.
- **Kết quả duyệt thực tế (`out/poc/review_queue_reviewed.csv`):**
  - Reviewer **Tran Anh Van** đã kiểm tra và phê duyệt quyết định `approve` (đồng ý HOLD 20/20 mẫu rớt mạng để tránh dữ liệu ảo).
  - Nhật ký ghi nhận vào `out/poc/run_info.json` với `n_reviewed: 20`.
- **Quyết định cuối cùng (`decision.json`):**
  - Quyết định: **`GO`** (Mã: `SYNC_PASS`).
  - Người ký duyệt: **Tran Anh Van - Sync Lead**.
  - Lý do: Tỷ lệ đồng bộ đạt 98.71% ($\ge 95\%$), 46 frame mất tín hiệu được tạm giữ an toàn.

---

## 08 · Reproduce & tests

```bash
# 1. Kiểm tra môi trường & hash dữ liệu (phải 0 FAIL):
make preflight

# 2. Chạy toàn bộ test suite (phải 0 failed):
make test

# 3. Chạy Baseline và PoC:
make baseline
make run
```

- **Môi trường tái lập:** macOS, Python 3.11.18, NumPy 2.4.6, Pandas 3.1.1, SciPy 1.17.1. Chạy 100% offline không cần Internet.

---

## 09 · Limitations & ethics

- **Đạo đức & An toàn AI trong xe tự hành:**
  - Tuyệt đối không được "suy diễn nội suy giả tạo" dữ liệu quán tính khi cảm biến IMU bị mất tín hiệu quá lâu ($>50$ ms), vì gia tốc và vận tốc góc có thể thay đổi đột ngột (xe va chạm hoặc trượt bánh). Việc tạm giữ `HOLD` là nguyên tắc an toàn hàng đầu.
- **Hạn chế:**
  - Nếu xe chạy trên mặt đường phẳng lì 100% không hề có một gờ giảm tốc nào, `motion_score` và `az` sẽ là đường thẳng nằm ngang $\rightarrow$ tương quan chéo sẽ kém chính xác. Khi đó cần kết hợp thêm cảm biến đo tốc độ bánh xe (Wheel Odometry).

---

## 10 · Team contribution

| Học viên | Git Author / Email | Module / Vai trò | Đóng góp & Minh chứng | Trình bày? |
|---|---|---|---|---|
| **Tran Anh Van** | `loversky02` / `bao232030333@lms.utc.edu.vn` | Lead / Core Algorithm / QA | • Thiết lập cấu hình `configs/lab.yaml`<br>• Triển khai thuật toán Cross-Correlation & Dropout filter trong `src/pipeline.py`<br>• Thực hiện vòng review người thật `review_queue_reviewed.csv`<br>• Hoàn thành báo cáo `group_report.md` | **Có** |

---

## 11 · References

1. **nuScenes Dataset:** Holger Caesar et al., *"nuScenes: A multimodal dataset for autonomous driving"*, CVPR 2020. (Giao thức đồng bộ timestamp đa cảm biến Camera - IMU - LiDAR).
2. **Scipy Signal Processing:** `scipy.signal.correlate` documentation for time-delay estimation using cross-correlation.
3. **CARLA Simulator Sensor Suite:** Synchronization between RGB Camera frames and IMU sensor streams in simulation environments.
