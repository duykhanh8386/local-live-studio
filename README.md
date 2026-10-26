# Local Live Studio

Web riêng chạy trên máy Windows, đọc trực tiếp video trong ổ đĩa và dùng FFmpeg trên chính máy đó để gửi livestream. Không cần VPS, Google Drive hoặc tải video lên web trước.

## Tải EXE và cập nhật từ GitHub

[Tải bản Windows mới nhất](https://github.com/duykhanh8386/local-live-studio/releases/latest) · [Tiến độ đóng gói](https://github.com/duykhanh8386/local-live-studio/actions)

Bản EXE dành cho **Windows 10/11 x64**. Python, các thư viện và FFmpeg/FFprobe nằm trong EXE; máy nhận không cần cài môi trường lập trình. Phần điều khiển Studio dùng Chrome hoặc Edge đã cài trên máy. Windows ARM, Windows 32-bit, macOS và Linux chưa có bản đóng gói.

1. Tải `LocalLiveStudio-Windows-x64.zip`, giải nén và mở `LocalLiveStudio.exe`. ZIP kèm hướng dẫn và thông báo giấy phép thành phần. Cũng có thể tải EXE riêng.
2. Giữ cửa sổ chương trình chạy; trình duyệt mở `http://127.0.0.1:8765`. Trong giao diện có nút **Thoát tool** để dừng chương trình, gồm các luồng live/tác vụ nền.
3. Dữ liệu bản EXE lưu ở `%LOCALAPPDATA%\LocalLiveStudio\data`, tách khỏi EXE. Đổi EXE không xóa lịch, stream key hoặc profile Studio. Có thể dùng `LIVE_DATA_DIR` nếu muốn chỉ định thư mục dữ liệu khác. Bản mã nguồn và portable cũ vẫn dùng thư mục dữ liệu của chúng; chọn lại file/key khi dùng EXE lần đầu, hoặc chuyển dữ liệu trên cùng máy/tài khoản khi chương trình đã dừng.

Trong phần **Cập nhật tool**: bấm **Kiểm tra cập nhật → Tải bản mới → Cài & khởi động lại**. Tool đọc SHA của commit mới nhất trên nhánh `main`, tìm bản phát hành `build-<SHA>` tương ứng và kiểm tra SHA-256/kích thước EXE tải về. Nếu GitHub chưa đóng gói xong commit đó, tool báo đang chờ; không tự chọn bản cũ hơn. Trước khi cài, tool kiểm tra lại commit trên GitHub và chặn khi có luồng live hoặc thao tác Studio đang chạy. Lượt tải có tiến độ trong giao diện.

Trình cập nhật chỉ thay EXE đang chạy, tạo bản dự phòng `.previous`, giữ thư mục dữ liệu và khởi động lại cùng cổng/số luồng. Thư mục chứa EXE cần cho phép tài khoản hiện tại ghi file. Log cài ở `data/updates/<commit>/install.log`; nếu bản mới không khởi động được, giữ lại `.previous` để phục hồi khi chương trình đã dừng. Bản mã nguồn có thể kiểm tra và mở trang tải nhưng không tự ghi đè Python hoặc mã nguồn.

Mỗi lần push lên `main`, workflow [Build Windows EXE](.github/workflows/windows-exe.yml) sẽ chạy kiểm thử, đóng gói trên Windows, kiểm tra EXE thật trong thư mục sạch rồi phát hành EXE/ZIP/checksum. Pull request chỉ kiểm thử và tạo artifact. Workflow phát hành dùng `GITHUB_TOKEN` của Actions với quyền `contents: write`; không cần chép token cá nhân vào repo. Không đưa `data`, phiên đăng nhập, stream key, video, log, bản build cục bộ hoặc thư mục QA lên Git.

Repo công khai tải/cập nhật không cần đăng nhập GitHub. Với repo riêng tư, tài khoản tải phải có quyền truy cập; cập nhật trong tool cần biến môi trường `LIVE_GITHUB_TOKEN` trên từng máy. Token này không lưu vào dữ liệu giao diện và không đóng vào EXE.

Đóng gói thủ công trên Windows: `python -m pip install -r requirements-build.txt`, `python packaging/fetch_ffmpeg.py`, rồi `python packaging/build_exe.py`. Gói FFmpeg có phiên bản và SHA-256 cố định trong `packaging/ffmpeg.json`. Chạy `python packaging/smoke_exe.py dist/windows/LocalLiveStudio.exe` để kiểm tra EXE trước khi chia sẻ.

## Tự cắt đoạn bị claim trên bản lưu và công khai

Tính năng mới áp dụng cách điều khiển trình duyệt của [Youtube-remove-copyright](https://github.com/vasani-arpit/Youtube-remove-copyright): mở phần bản quyền trong Studio, chọn **Trim out segment / Cắt bỏ đoạn**, gửi xác nhận, chờ YouTube xử lý và đọc lại kết quả. Đây là thao tác **cắt vĩnh viễn cả hình lẫn âm thanh tại đoạn bị claim** trên bản lưu YouTube. File nguồn trên máy được giữ nguyên. Xem [hướng dẫn cắt nội dung bị claim của YouTube](https://support.google.com/youtube/answer/2902117?hl=en).

1. Chạy `start.bat` để cập nhật thư viện, rồi mở phần **Xử lý bản lưu YouTube** cuối trang. Máy cần có Chrome hoặc Edge; không cần tải thêm Chromium hay ChromeDriver.
2. Bấm **Mở Studio / Đăng nhập**. Đăng nhập trực tiếp trên trang Google và chọn đúng kênh trong cửa sổ riêng do tool mở, rồi quay lại web bấm **Đăng nhập xong — chạy nền**. Tool kiểm tra Channel ID, đóng cửa sổ đăng nhập, mở lại cùng profile ở chế độ nền và xác minh phiên trước khi báo kết nối. Khi đang đăng nhập, hàng chờ cắt tạm dừng để bạn hoàn tất xác minh Google. Có nút **Hủy đăng nhập** để thoát bước này.
3. Với lịch live mới: mở **Xử lý bản lưu YouTube sau live**, nhập đúng **Video ID hoặc link phiên live**, bật **Tự cắt các đoạn bị claim khi live kết thúc**. Mặc định bật **Tự chuyển sang Công khai** theo yêu cầu. Stream key không cung cấp Video ID; mỗi phiên live mới cần ID của chính phiên đó.
4. Bật **Auto-stop** trong YouTube Studio hoặc tự kết thúc phiên live. Khi máy phát hoàn tất hoặc bạn bấm Dừng, tác vụ được đưa vào hàng chờ. Tool vẫn xác nhận live đã kết thúc trên YouTube trước khi cắt; việc FFmpeg dừng chưa đủ để xác nhận.
5. Với bản lưu có sẵn: dán Video ID/link ở phần cuối trang, chọn chế độ công khai và đồng ý cắt vĩnh viễn, rồi bấm **Thêm bản lưu cần xử lý**.

Sau khi lưu phiên, tool xử lý từng bản lưu bằng trình duyệt chạy nền; các luồng phát FFmpeg vẫn chạy độc lập. Có thể đóng tab web local; cần giữ chương trình đang chạy. Sau mỗi lệnh cắt, tool đọc lại khoảng mỗi 60 giây. Chỉ khi Studio báo không có claim qua hai lượt đọc và một lần kiểm tra ngay trước công khai, tool mới đổi quyền hiển thị. Sau khi lưu quyền hiển thị, tool tải lại trang để xác nhận **Công khai**. Nếu bỏ tùy chọn công khai, tool giữ quyền hiển thị hiện tại. Tool không tự đổi video đang Công khai sang Riêng tư trong lúc chờ.

Trạng thái **Đang xử lý** chưa phải thành công. Khi phiên hết hạn, bấm **Mở Studio / Đăng nhập**, đăng nhập/chọn kênh và bấm **Đăng nhập xong — chạy nền**; các tác vụ cần đăng nhập sẽ tự đọc lại trạng thái để tiếp tục. **Cần kiểm tra** có log giải thích; xử lý nguyên nhân rồi bấm **Tiếp tục** tại dòng đó. Nút **Dừng tự động** ngừng các bước tiếp theo của tool, không hủy được lệnh chỉnh sửa đã gửi cho YouTube. Nút **Kiểm tra lại** cho phép kiểm tra tác vụ đã hoàn tất; thêm lại cùng Video ID không tạo tác vụ trùng hay đổi lựa chọn đã lưu.

Hàng chờ, số lượt cắt và lệnh đang chờ xác minh nằm trong `data/streams.db`. Khi khởi động lại, tool đọc trạng thái thực tế thay vì tự gửi lại cùng đoạn cắt. Nếu kết quả không rõ hoặc claim vẫn còn sau xử lý, tác vụ chuyển sang **Cần kiểm tra**. Mỗi tác vụ tự chờ tối đa 24 giờ và gửi tối đa 20 lượt cắt, sau đó cần kiểm tra thủ công.

Phiên đăng nhập nằm trong profile Chrome/Edge `data/studio-profile`; Channel ID, thời điểm xác minh và chế độ nền lưu trong `data/streams.db`. Khi mở lại chương trình, tool dùng profile đã lưu; Google có thể yêu cầu đăng nhập/xác minh lại. Tool kiểm tra video thuộc kênh đã kết nối trước khi sửa. Đây là phiên đăng nhập trình duyệt để thao tác Studio, không phải OAuth token cấp thêm quyền. Tool thực hiện quy trình cắt/công khai đã chọn; không tự quyết định thao tác khác trên tài khoản. Mật khẩu nhập trên trang Google, không gửi vào API của web local. Không chia sẻ thư mục `data` hoặc đóng gói profile này cho người khác. Có thể đặt biến `LIVE_STUDIO_BROWSER` tới file `chrome.exe` / `msedge.exe` nếu tool không tìm thấy trình duyệt. Nếu tiến trình Studio cũ còn chạy sau sự cố, đóng tiến trình riêng của tool trước khi mở lại.

Tính năng xử lý claim của **bản lưu đã kết thúc**, không bảo vệ buổi live đang phát và không khôi phục video bị gỡ. Bản lưu từ 6 giờ trở lên, claim không cho phép cắt, phiên hết hạn hoặc giao diện Studio chưa nhận diện được sẽ cần kiểm tra thủ công. “Hoàn tất” chỉ phản ánh kết quả tại lần đọc đó, không bảo đảm không phát sinh claim mới.

Kiểm thử bằng `python -X utf8 -m unittest tests.test_archives tests.test_studio_browser tests.test_studio_profile tests.test_live tests.test_startup -v`; kiểm tra giao diện bằng `python -X utf8 -m tests.browser_archive_qa`. Bài kiểm thử trình duyệt dùng Chrome/Edge với trang Studio mô phỏng và chặn mọi yêu cầu mạng bên ngoài; bài kiểm tra profile dùng cookie giả để xác nhận phiên được giữ khi đổi sang chạy nền. Không đăng nhập hay chỉnh video thật. Giao diện Studio thực tế theo tài khoản/ngôn ngữ có thể khác; tool sẽ dừng khi không nhận diện được thao tác.

Bản mã nguồn đã thêm tính năng này; các ZIP portable đã tạo trước đó không tự cập nhật. Khi đóng gói bản mới bằng `portable/build_portable.py`, Playwright được kèm trong runtime và Chrome/Edge dùng bản cài trên máy nhận.

## Bản portable để gửi sang máy khác

Gửi ZIP `dist/LocalLiveStudio_Portable_Windows_x64_*.zip`, giải nén **toàn bộ**, rồi nhấp đúp **START_LIVE.bat**. Bản này kèm Python, FFmpeg/FFprobe và các thư viện, không cần cài môi trường hay tải thêm thư viện trên máy nhận. Dành cho **Windows 10/11 64-bit**, dùng trình duyệt mặc định của máy.

Khởi động web có thể thực hiện offline; livestream lên nền tảng cần Internet. Gói có danh sách ban đầu trống, không chứa lịch, stream key, log hoặc video của máy đóng gói. Chọn lại video và nhập key tại máy nhận. Xem `HUONG_DAN.txt` trong ZIP; **CHECK_SETUP.bat** kiểm tra các thành phần và SHA-256 của file trong gói.

Các bước dưới đây dành cho bản mã nguồn; với bản portable, dùng launcher trong ZIP.

Nếu máy nhận báo **ERR_CONNECTION_REFUSED**, giữ cửa sổ **START_LIVE.bat** mở rồi chạy **DIAGNOSE_LIVE.bat**. Công cụ kiểm tra TCP/HTTP, thành phần và quyền ghi, lưu kết quả ở `data/diagnostic.txt`; lỗi khởi động nằm trong `data/startup.log`. Bản mới chỉ in dòng **Máy chủ đã sẵn sàng (HTTP 200)** và mở trình duyệt sau khi web phản hồi thành công. Khi dùng cổng khác, chạy `DIAGNOSE_LIVE.bat --port 8770` tương ứng.

## Chạy ngay

1. Nhấp đúp **start.bat** trong thư mục này.
2. Trình duyệt mở **http://127.0.0.1:8765**. Nếu không tự mở, nhập địa chỉ này.
3. Giữ cửa sổ chương trình mở trong lúc chờ lịch và phát live. Đóng tab trình duyệt vẫn tiếp tục phát; **Ctrl+C** trong cửa sổ chương trình sẽ dừng các luồng do ứng dụng chạy.

Máy này đã có Python, FFmpeg và FFprobe. `start.bat` tự cài FastAPI/Uvicorn/Playwright nếu thiếu. Nếu chuyển sang máy khác, cài Python 3.12 trở lên và [FFmpeg](https://ffmpeg.org/download.html), thêm thư mục chứa `ffmpeg.exe` và `ffprobe.exe` vào PATH. Hoặc đặt biến `LIVE_FFMPEG` / `LIVE_FFPROBE` bằng đường dẫn đầy đủ tới từng file.

Chạy bằng PowerShell:

```powershell
Set-Location -LiteralPath 'C:\tool tuat videos\local_livestream'
python -m pip install -r requirements.txt
python app.py
```

Đổi cổng hoặc giới hạn số luồng:

```powershell
python app.py --port 8770 --max-streams 2 --no-browser
```

Mặc định tối đa **4 luồng đồng thời**; khả năng chạy thực tế phụ thuộc CPU/GPU và băng thông upload của máy. Lịch đến hạn khi hết chỗ sẽ hiển thị **Chờ máy phát**. Một stream key không được dùng đồng thời bởi hai luồng trong ứng dụng.

## Thiết lập một buổi live

1. Trong YouTube Studio, tạo phiên livestream dùng encoder. Chọn tiêu đề và quyền riêng tư tại Studio; lấy **stream key**. Nếu muốn YouTube tự mở/đóng buổi live theo tín hiệu, thiết lập **Auto-start / Auto-stop** tại Studio. Xem [hướng dẫn encoder của YouTube](https://support.google.com/youtube/answer/2907883?hl=en) và [cơ chế tự bắt đầu/kết thúc](https://developers.google.com/youtube/v3/live/life-of-a-broadcast).
2. Trên web local, nhập tên luồng và stream key. Bấm **Chọn từ thư mục**, chọn ổ đĩa/thư mục rồi chọn video, hoặc dán đường dẫn đầy đủ tới video.
3. Chọn **Live theo giờ** và thời lượng `HH:MM:SS`, hoặc **Live 24/7**. Mỗi luồng dùng một file video; file tự lặp khi hết. 24/7 chạy đến khi bạn bấm **Dừng**.
4. Chọn ngày giờ bắt đầu theo **giờ Việt Nam (UTC+7)**, bitrate và tùy chọn mã hóa.
5. Bấm **Thêm lịch phát** để chờ đến giờ, hoặc **Lưu & live ngay** để chạy ngay. Theo dõi tiến độ, trạng thái và log ở bảng phía dưới.

URL mặc định dành cho YouTube là `rtmps://a.rtmps.youtube.com:443/live2`. Có thể thay URL RTMP/RTMPS của nền tảng khác trong **Tùy chọn máy phát**. Chỉ nhập URL máy chủ vào trường URL; nhập key riêng ở trường stream key.

Ứng dụng gửi tín hiệu encoder, chưa kết nối tài khoản YouTube để tự tạo sự kiện, đổi Public/Private hoặc đọc số người xem. Trạng thái **Đang phát** cho biết FFmpeg đang xuất tín hiệu; kiểm tra YouTube Studio để xác nhận buổi live đã lên sóng. Các thiết lập khuyến nghị của nền tảng có trong [tài liệu encoder của YouTube](https://support.google.com/youtube/answer/2853702?hl=en).

## Bộ mã hóa và video

| Lựa chọn | Khi sử dụng |
|---|---|
| CPU · H.264 | Mặc định; chuyển video sang H.264/AAC, 30 fps, keyframe 2 giây. Chọn bitrate và 720p/1080p/kích thước gốc. |
| NVIDIA · NVENC | Máy có GPU NVIDIA và driver hỗ trợ. Nếu không dùng được, log sẽ báo lỗi; chọn lại CPU. |
| Copy · giữ file gốc | Ít tốn CPU; cần video H.264 với audio AAC/MP3. Giữ bitrate, kích thước, fps và keyframe của file gốc. Hãy dùng file có thông số phù hợp nền tảng. |

Hỗ trợ MP4, MKV, MOV, AVI, WEBM, M4V, TS có thời lượng hợp lệ. Video không có audio được thêm audio im lặng. File nằm ở vị trí cũ, không bị upload, sao chép hay xóa khi xóa lịch. Hiện chưa hỗ trợ playlist nhiều file cho cùng một luồng.

Máy phải bật, không Sleep, ổ chứa video còn truy cập được, ứng dụng đang chạy và có mạng để tự phát đúng giờ. Lịch theo thời lượng mở muộn sẽ phát phần thời gian còn lại; nếu toàn bộ thời gian đã qua thì báo **Lỡ lịch**. Chạy thủ công bắt đầu lại một phiên với đủ thời lượng đã đặt. Khi kết nối lỗi, nếu bật tự thử lại thì ứng dụng thử tối đa 5 lần, chờ 2/4/8/16/30 giây; mỗi lần nối lại phát video từ đầu.

## Quản lý lịch và dữ liệu

- Danh sách có ID, tên video, thời gian, key đã che, tiến độ, trạng thái và nút chạy/dừng/sửa/xóa/log.
- Dừng lịch chưa chạy sẽ hủy tự phát lịch đó, vẫn giữ trong danh sách; **Chạy ngay** hoặc sửa lịch để chạy lại.
- Muốn sửa/xóa luồng đang chạy, bấm **Dừng** trước. Xóa lịch hoàn tất chỉ xóa các mục **Hoàn tất** và **Lỡ lịch**.
- Có tìm kiếm, lọc trạng thái/kiểu live và xuất CSV; CSV không chứa stream key.
- Lịch và 300 dòng log gần nhất của từng luồng nằm trong `data/streams.db`. Key được mã hóa bằng **Windows DPAPI**, gắn với tài khoản Windows hiện tại, và không trả nguyên key về bảng hay log.
- Mở lại ứng dụng giữ nguyên lịch. Các luồng đang chạy khi lần trước đóng/ngắt ứng dụng hiện **Bị ngắt**; bấm chạy để tiếp tục. Lịch tương lai vẫn tự chạy khi đến giờ.
- Trên Windows, các FFmpeg của ứng dụng được gắn vào nhóm tiến trình riêng để kết thúc cả khi máy chủ local bị đóng đột ngột. FFmpeg của ứng dụng khác không bị tác động. Cơ chế này dùng [Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects).
- Ứng dụng chỉ lắng nghe trên `127.0.0.1`. Không cần mở cổng router.

Có thể chọn nơi lưu khác bằng biến môi trường `LIVE_DATA_DIR`. Sao lưu thư mục dữ liệu khi chương trình đã tắt. Nếu chuyển dữ liệu sang tài khoản Windows khác, cần nhập lại stream key.

## Kế hoạch đã thực hiện

1. Giao diện web riêng có form thiết lập và bảng lịch, theo phong cách nền sáng, thẻ bo góc và nút nâu trong ảnh tham khảo.
2. API Python đọc folder/file trên máy, kiểm tra video, lưu SQLite và bảo vệ key bằng DPAPI.
3. Scheduler theo giờ Việt Nam, mỗi luồng có tiến trình FFmpeg riêng, lặp video, giới hạn số luồng và chống trùng key.
4. Theo dõi tiến độ, dừng từng luồng, kết nối lại, log, sửa/xóa và launcher Windows.
5. Kiểm tra API, vòng đời tiến trình, phát video thật vào file và máy nhận RTMP trên localhost, cùng thao tác giao diện trình duyệt.

Cấu trúc chính: `app.py` (khởi động), `live/api.py` (API), `live/engine.py` (scheduler và FFmpeg), `live/media.py` (video/mã hóa), `live/processes.py` (quản lý tiến trình Windows), `live/storage.py` (SQLite/key), `static/` (giao diện). Có thể chỉnh độc lập với tool Tuất Videos hiện tại.

## Kiểm tra

```powershell
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

Các kiểm tra tạo video mẫu, dùng dữ liệu riêng trong `qa/`, phát vào file hoặc RTMP localhost; không dùng key thật và không phát lên YouTube. NVENC và livestream ra nền tảng cần kiểm tra thêm trên GPU/tài khoản đích.

Kiểm tra giao diện tùy chọn (cần Chrome và ChromeDriver tương thích):

```powershell
python -m tests.browser_qa --driver 'C:\duong-dan\chromedriver.exe' --chrome 'C:\Program Files\Google\Chrome\Application\chrome.exe'
```

Ảnh kiểm tra giao diện nằm ở `qa/desktop.png`, `qa/mobile.png`; kết quả ở `qa/browser-report.json`. Máy chủ kiểm tra tự tắt khi kết thúc.

Kiểm tra trên máy hiện tại: **15 kiểm tra bộ phát/API đạt**, thao tác trình duyệt Chrome đạt và không có lỗi JavaScript. Đã thử mã hóa, Copy với video không audio, lịch tự chạy, dừng/đóng ứng dụng và RTMP localhost. Chưa phát thử lên YouTube bằng stream key thật.
