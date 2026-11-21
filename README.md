# Phòng agent — nhiều AI trong một tab

Một server nhỏ chạy local. Bạn mở một tab, gõ `"tên` để gọi agent, agent trả lời (stream), và có thể gọi agent khác bằng cách viết `"tên` trong câu trả lời của nó.

## Chạy

```bash
pip install -r requirements.txt
# điền base_url / model trong config.yaml, rồi đặt key (nếu cần):
export MUSE_KEY_GA=...  MUSE_KEY_VIT=...  ANTHROPIC_API_KEY=...
python hub.py
# mở http://127.0.0.1:8765
```
Windows PowerShell: `$env:ANTHROPIC_API_KEY="..."`

## Dùng

- `"ga xem giúp đoạn này` → chỉ ga trả lời.
- `"ga nhờ "vit soi lại` → ga trả lời, nếu ga viết `"vit` thì vit được gọi tiếp.
- Tên không có dấu `"` phía trước chỉ là nhắc tới, không đánh thức.
- `/clear` xóa toàn bộ phòng.

## Bản đồ (cột trái)

Ý tưởng mượn từ [lot-map](https://github.com/alexeyw/lot-map): tâm là phòng, mỗi agent và bạn là một trạm.

- **Xung:** ai gọi ai thì một chấm bay từ trạm gọi sang trạm được gọi; khi agent trả lời xong, một chấm rỗng bay về tâm.
- **Vòng quay quanh trạm:** agent đang chạy. Nhiều lượt gọi cùng lúc vẫn chỉ hiện một vòng.
- **Vòng quanh tâm:** số tin agent liên tiếp trên `max_bot_chain`. Vàng từ 70%, đỏ từ 90%. Bạn nhắn tin là về 0.
- **Nan hoa dày dần:** trạm nào nói nhiều thì đường nối tới tâm đậm và dày hơn.
- **Chấm đỏ trên trạm:** agent vừa lỗi (thiếu key, sai địa chỉ…), tự mất khi bạn nhắn tin tiếp.
- Bấm vào trạm để chèn `"tên` vào ô nhập. Bản đồ ẩn trên màn hình hẹp (điện thoại); danh sách tên vẫn còn.

## Chống vòng lặp

- Quá `max_bot_chain` tin agent liên tiếp (không có tin của bạn xen vào) thì hub ngắt và chờ bạn.
- `can_call: false` trong config → agent đó không gọi được agent khác.
- Mỗi agent xử lý từng lượt gọi một (hàng đợi riêng).

## Thêm agent

Thêm một khối vào `agents:` trong `config.yaml`. Hai loại adapter:
- `openai_compat`: muse2api, OpenAI, Gemini (endpoint tương thích), DeepSeek, Groq, OpenRouter, Ollama, LM Studio…
- `anthropic`: Claude qua Anthropic API (cần API key, tính tiền theo lượng dùng).

Tên agent có thể là chữ có dấu (ví dụ `gà`), gõ `"gà` để gọi.

## Lưu ý

- Mặc định chỉ lắng nghe `127.0.0.1`. Nếu mở ra mạng LAN/Internet, đặt sau reverse proxy có xác thực, vì hub chưa có đăng nhập.
- Lịch sử lưu trong `room.db` (SQLite) cùng thư mục.
