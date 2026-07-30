# Camera Bot

## Ghi chú về Git và ignore file

Nếu bạn muốn bỏ qua các file tự động sinh ra như file Python bytecode (.pyc), hãy làm theo các bước sau:

1. Thêm rule vào file .gitignore.
2. Nếu file đã từng được Git track trước đó, cần bỏ khỏi index bằng lệnh:
   git rm -r --cached <path>; sample: git rm -r --cached --ignore-unmatch __pycache__
3. Commit lại để thay đổi có hiệu lực.

Ví dụ thường dùng:
- __pycache__/
- *.pyc
- *.pyo

Lưu ý: .gitignore chỉ ảnh hưởng tới các file chưa được track. Với file đã được add trước đó, cần untrack bằng git rm --cached.
