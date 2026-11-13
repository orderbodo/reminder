# Telegram Persistent Reminder Bot (Supabase + Render + Cron)

Bot nhắc việc kiên trì trên Telegram, hỗ trợ lưu trữ Supabase đám mây 24/7, tự động snooze và điểm tin hằng ngày.

---

## 📌 CÚ PHÁP NHẬP CÔNG VIỆC CHUẨN

Ngăn cách các phần bằng dấu gạch ngang `-` (hoặc `|`):

```text
[Tên việc] - [Khung giờ] - [Snooze lặp lại]
```

### Ví dụ thực tế:

1. **Đầy đủ cả giờ bắt đầu, giờ kết thúc và chu kỳ snooze:**
   ```text
   Làm slide - từ 11h đến 15h - snooze mỗi 30p
   ```
   * *Ý nghĩa:* Bot bắt đầu nhắc lúc 11:00, kết thúc lúc 15:00, lặp lại mỗi 30 phút.

2. **Chỉ hẹn giờ bắt đầu:**
   ```text
   Họp team - lúc 14h
   ```
   * *Ý nghĩa:* Im lặng cho đến đúng 14:00 mới bắt đầu nhắc (chu kỳ mặc định 1h).

3. **Việc lặp lại thường xuyên (không giới hạn giờ):**
   ```text
   Uống nước - snooze mỗi 45p
   ```
   * *Ý nghĩa:* Nhắc ngay lập tức, cứ 45 phút nhắc lại 1 lần nếu chưa bấm xong.

4. **Nhập nhanh việc thông thường:**
   ```text
   Đọc sách 30 trang
   ```
   * *Ý nghĩa:* Nhắc ngay, tự động snooze 1 tiếng nếu không trả lời.

---

## ⏰ CƠ CHẾ SNOOZE / NAGGING

- **Khi có tin nhắn nhắc:**
  - Bấm `[✅ Xong]` khi hoàn thành.
  - Bấm `[⏰ Snooze]` để hoãn đúng theo chu kỳ bạn cài cho việc đó (ví dụ 30p hoặc 1h).
  - Bấm `[🗑 Xoá]` để huỷ việc.
- **Tự động snooze:** Nếu không trả lời sau 2 phút, bot sẽ **tự động snooze** đúng theo chu kỳ của việc đó.
- **Hoãn tự do bằng tin nhắn:**
  - `30p` hoặc `2h`
  - `pause slide 2h`
  - `done slide`

---

## 📋 CÁC LỆNH HỆ THỐNG

- `/start` : Mở lại bảng hướng dẫn cú pháp chi tiết.
- `/list` : Xem danh sách việc kèm thời gian hẹn và chu kỳ.
- `/pause` : Tạm dừng toàn bộ nhắc nhở (1h, 3h, 4h...).
- `/resume` : Tiếp tục nhận thông báo ngay lập tức.
- 🌙 **Giờ yên lặng:** Tự động tắt nhắc nhở từ **22:00 đến 09:00 sáng**.
