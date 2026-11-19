# Git Sync Tool

Mọi chế độ đều bỏ toàn bộ `.github/` từ A và giữ `.github/` sẵn có của B. Khi không chọn commit, không message và không base branch: B chưa có thì lấy snapshot mới nhất của A, bỏ `.github/`, tạo đúng một commit dùng tiêu đề/tác giả của commit mới nhất; B đã có thì đồng bộ các commit mới, giữ code riêng của B. Giữ tiêu đề/tác giả gốc, không tự thêm tiêu đề `Initialize...`; hash có thể khác vì đã loại `.github/`.

Minimal Windows Tkinter app. Requires Python 3.10+ with Tkinter and Git on PATH; no third-party Python packages.

## Run

Double-click `run.bat`, or run:

```powershell
cd C:\laragon\www\git-sync-tool
python sync_tool.py
```

## Basic workflow

The **Source A — From** and **Source B — To** groups each contain Folder / Browse, URL and Branch. Changes flow from A's branch into B's branch. **Switch A ↔ B** swaps both sources, branches and folder paths, and clears the loaded list.

The **New commit message** field is optional:

- **Trống, Base branch cũng trống:** không cần tải danh sách hoặc chọn commit. Nếu nhánh B chưa có, lấy snapshot code mới nhất của A, bỏ `.github/`, tạo một root commit duy nhất dùng tiêu đề và tác giả của commit mới nhất (không thêm mô tả). Không áp lại từng commit lịch sử, không dùng nhánh mặc định B làm nền. Nếu B đã có, áp các commit mới của A lên B theo thứ tự, giữ code riêng của B và tiêu đề/mô tả/tác giả gốc. Dãy commit mới được xác định từ lịch sử chung hoặc trạng thái sync đã lưu. Nếu không có cả hai, lần đầu chỉ áp commit mới nhất của A; các lần sau áp các commit phát sinh sau đó. Commit chỉ thay đổi `.github/` được bỏ qua, commit có cả code và `.github/` chỉ nhập phần code. Không tự thêm tiêu đề `Initialize...`, không force-push. Hash có thể khác do lọc file và áp lên nền B. Với B đã tồn tại, nếu toàn bộ lịch sử A không thay đổi `.github/` và có thể fast-forward an toàn thì push trực tiếp để giữ nguyên hash. Conflict thật sự thì dừng, không push. Sync lại khi không có thay đổi không tạo thêm commit.
- **Trống nhưng có Base branch:** áp các commit mới lên nền B, giữ tiêu đề/tác giả gốc nhưng hash có thể khác; toàn bộ `.github/` từ A bị loại trừ.
- **Có nhập tiêu đề:** bấm **Load commits from A**, chọn commit bằng Ctrl/Shift-click, nhập tiêu đề rồi bấm Sync. Các thay đổi được gộp thành một commit mới dùng Git identity đã cấu hình. Commit message chỉ chứa dòng tiêu đề đầu tiên bạn nhập, không có mô tả, hash nguồn hay nội dung tự thêm. Phải chọn ít nhất một commit khi nhập tiêu đề.

**Base branch (optional) trong Source B:** nhập tên nhánh gốc trên server B, ví dụ `project`. Nếu B1 chưa tồn tại, tool lấy phiên bản mới nhất của `project` trên server, dùng làm nền tạo B1, rồi áp các thay đổi từ A lên đó. Các file riêng của nhánh gốc được giữ lại. Chọn nhiều commit và nhập tiêu đề vẫn gộp thành một commit mới, chỉ có tiêu đề đã nhập. Nếu B1 đã tồn tại, ô này không ảnh hưởng đến code nền. Nếu nhánh gốc không có trên server, tool báo lỗi và không push, không tự chuyển sang nhánh mặc định. Để trống để dùng hành vi cũ. Giá trị này được lưu/khôi phục trong lịch sử; Switch A ↔ B sẽ xóa giá trị vì đây là cấu hình riêng của B. Khi chỉ định nhánh gốc, các thay đổi được áp dưới dạng patch nên vẫn có thể conflict; tool dừng mà không ghi đè. Nếu A và nhánh gốc không có lịch sử chung, lần đầu cần chọn commit và nhập tiêu đề thay vì sync tự động.

**Squash sang nhánh B mới (không nhập Base branch):** nếu nhánh đích chưa tồn tại và các commit được chọn liên tiếp trên cùng một chuỗi cha-con, tool lấy toàn bộ snapshot ở commit cuối được chọn để tạo đúng một commit mới với message đã nhập. Thay đổi của commit sau thay thế commit trước; các file đã thêm ở commit trước vẫn còn nếu chưa bị xóa. Không áp patch lên code nền của B nên không phát sinh conflict kiểu đó. Commit mới nằm trên nhánh mặc định của B, hoặc là root commit duy nhất nếu B rỗng. Các file chỉ có ở B không được giữ lại, ngoại trừ `.github/` của B; toàn bộ `.github/` từ A vẫn bị loại trừ. Commit mới hơn nhưng không được chọn không được nhập. A không bị sửa. Nếu chọn các commit không liên tiếp hoặc nhánh B đã tồn tại, tool vẫn áp các thay đổi được chọn theo thứ tự và dừng an toàn khi conflict.

Chế độ được xác định bởi message và Base branch. Khi áp thay đổi tự động lên Base branch, lần đầu cần lịch sử chung hoặc baseline đã lưu. Nếu không xác định được dãy commit, hãy chọn commit và nhập tiêu đề. Với tùy chọn để trống và B đã tồn tại nhưng không có lịch sử chung/baseline, tool chỉ áp commit mới nhất của A.

Đồng bộ mặc định tạo nhánh B chưa tồn tại bằng một snapshot mới nhất từ A đã bỏ `.github/`, không dựng lại lịch sử và không phụ thuộc nhánh mặc định của server B. Nếu nhánh B bị xóa, lần Sync mặc định tiếp theo tạo lại từ A và vẫn lọc `.github/`. Với chế độ gộp commit, nhánh B mới vẫn dùng nhánh mặc định B làm cha; nhánh gốc chỉ định được ưu tiên nếu có.

Nếu B rỗng, đồng bộ mặc định tạo một root commit từ snapshot mới nhất, giữ tiêu đề/tác giả mới nhất của A, bỏ `.github/`, không cần khởi tạo thủ công. Kế hoạch cũ chưa push đang dựng lại lịch sử được sao lưu và thay bằng snapshot để không phải xử lý hàng trăm commit. Các commit nguồn trước snapshot được đánh dấu đã nhập để lần sau chỉ sync phần mới. With a custom message and a consecutive selection, the final selected source snapshot becomes one new root commit (workflows excluded). For a nonconsecutive selection, code before the oldest selected commit is imported as a workflow-free baseline snapshot, then only selected code changes are combined into one new commit. If the source root itself is selected, the result is a new root commit without an internal helper parent. Changes outside the selection after the chosen base are not silently included. The destination repository must already exist and allow your account to push; this creates Git branches, not hosting-service repositories.

**Load commits from A:** chỉ fetch nhánh A đã nhập, không fetch B hay các nhánh A khác. Khi Folder trống, danh sách dùng cache preview riêng và yêu cầu server bỏ nội dung file (`blob:none`), và giới hạn độ sâu lịch sử bằng `--depth=15`. Server không hỗ trợ lọc vẫn có thể gửi đầy đủ dữ liệu của nhánh; lần đầu với repo lớn vẫn có thể lâu. Không giới hạn băng thông. Chỉ hiển thị tối đa 15 commit mới nhất, kèm tiến độ Git trong log. Cache preview không được dùng làm checkout Sync để giới hạn 15 commit không làm thiếu lịch sử khi đồng bộ. Khi có folder A hợp lệ, chỉ cập nhật nhánh A được chọn bằng cấu hình Git của folder đó, không đổi working files.

**Tối ưu dữ liệu khi Sync:** chỉ fetch nhánh A đã chọn. Với B, chỉ đọc danh sách tên/hash nhánh từ server rồi fetch nhánh đích nếu đã tồn tại; nếu chưa có thì chỉ fetch nhánh nền cần dùng (base branch, nhánh mặc định hoặc nhánh dự phòng theo quy tắc bên dưới). Không tải toàn bộ các branch. Nếu B có folder Git hợp lệ, cập nhật đúng nhánh cần thiết trong folder đó rồi nhập dữ liệu có sẵn qua ổ đĩa vào cache; không dùng dữ liệu cũ thay thế khi server lỗi. Với A chỉ có URL, yêu cầu `blob:none`: giữ đầy đủ lịch sử commit/cây thư mục để kiểm tra an toàn, chỉ lấy nội dung file khi Git cần áp patch hoặc lấy snapshot. Server không hỗ trợ partial fetch vẫn có thể gửi toàn bộ nội dung lịch sử của nhánh A; tạo snapshot mới cũng cần tải các file của snapshot đó. Đây là giảm dữ liệu tải, không phải giới hạn tốc độ mạng.

Both servers are checked before syncing. New default branches use a single latest-tree snapshot, including the final code from merges. Safe fast-forward on an existing B preserves original commits unchanged. Replaying new commits onto an existing divergent B uses first-parent deltas for merges; explicit selection of a merge commit still stops for a mainline decision. Rewritten histories and conflicts stop for review. If one commit in a batch conflicts, the whole batch is discarded in the private checkout and nothing is pushed. Pushes never force-update B, so a concurrent B update is rejected safely. A pending push is saved and recovered or stopped conservatively on retry; check the log before starting a new selection after a retry.

## Optional folders and latest server code

Only the exact folder root is checked; no child or parent directory search is performed. Source A remains a read-only convenience cache: if it has no Git metadata, its URL is used through the private cache. For Source B, a missing or empty folder is created/initialized automatically on Sync and connected to the entered B server URL. A preinitialized empty Git repo is also supported. If a nonempty B folder has files without Git metadata, those files are preserved and the server sync uses the private cache. Enter the server URL manually when Browse cannot infer one. Loading the commit list does not initialize B.

**Browse…** fills the corresponding URL if an `origin` or only remote is available. If no URL is found, it keeps your current URL and shows no popup. Browsing does not fetch or push. URLs remain manually editable.

When a valid folder is supplied, the tool fetches the latest server refs using that repo's Git/SSH/credential configuration, then imports the fetched objects into its private checkout. A new server commit on `dev` is visible even if the local `dev` branch is stale. Source A working files remain untouched. After a successful server sync, local B is refreshed from its server branch: a fresh/empty B folder is checked out and filled with code; a clean checked-out B target branch advances only by fast-forward. Uncommitted/ignored files and divergent local commits are preserved. If another local branch is currently checked out, the B target branch is made available without switching the working branch. Dedicated `refs/git-sync-tool/` and `refs/git-sync-tool-push/` refs hold fetched and outgoing objects. Push always uses B's configured URL. A server access failure stops the operation; stale local data is not substituted. If URL itself is a local path, that local repository is the authoritative source rather than a separate server.

Even when the server reports no new source commits, the local B setup/update still runs. Thus an empty B folder can be populated from an already-synced server branch without creating duplicate commits. The hosting-service repository at the B URL must already exist and allow access; local `git init` does not create a GitHub repository.

## Git identity and access

Configure your identity:

```powershell
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

Prepare SSH keys or Git Credential Manager access to both servers. Interactive credential prompts are disabled. Repo-specific transport settings are reused when valid folders are supplied. Local files alone do not grant server access. Errors identify the failed fetch/push step and retain Git's original error. No global Git/SSH settings or ownership checks are changed. Avoid credentials in URLs.

### GitHub workflow permission rejection

Mọi lần sync mới đều loại `.github/` khỏi thay đổi nhập từ A, kể cả lịch sử cha khi tạo nhánh mới. Vì vậy không cần quyền `workflow` cho những file đã loại. `.github/` có sẵn của B vẫn được giữ. Kế hoạch push chưa thành công của phiên bản cũ được sao lưu thành `pending-before-workflow-filter-<id>.json` rồi dựng lại với bộ lọc mới nếu B chưa đổi. Kiểm tra cuối chặn push nếu lịch sử mới vẫn đưa thay đổi `.github/` lên B. Không sửa lịch sử đã push. Nếu vẫn thấy lỗi OAuth workflow, kiểm tra đang chạy phiên bản mới trước khi cấp thêm quyền.

For HTTPS, authorize workflow access for the credential provider/token that Git is actually using. A classic personal access token needs the relevant repository scope plus `workflow`; a fine-grained token for the destination repository needs appropriate Contents and Workflows write permissions. Use your credential provider's normal sign-in/token update flow; do not put tokens into URLs or saved history. See [OAuth scopes](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps) and [token management](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).

Alternatively use SSH only after confirming the key's GitHub account has access to this specific destination repo. `ssh -T git@github.com` verifies the account, but successful authentication alone does not prove repository permission. GitHub can return Repository not found for an inaccessible private repo. After access is fixed, retry Sync; do not delete the saved pending state.

## History and state

Only completed Sync operations are saved. Running and failed attempts are never written to history, and a failed retry does not replace the last completed entry. Choose an entry in **Sync history** and click **Open history** to restore both URLs, branches, folders, the optional message and selected hashes. If a message is present, the source list refreshes and saved reachable commits are reselected, limited to the newest 15 commits. Older saved selections are skipped with a log message; opening history does not download additional history. Opening history never pushes automatically.

History keeps one entry for each A/B URL, branch and folder configuration, up to 100 unique configurations, in `%LOCALAPPDATA%\GitSyncTool\history.json`. Repeating a successful Sync updates that entry with the newest successful message/selection and time instead of adding another row. Time, message and selected commits do not make a duplicate configuration unique. Equivalent GitHub HTTPS/SSH URLs and Windows folder case/slash variations are normalized. A completed no-op Sync may update an entry; failed attempts are ignored. Old Running/Failed rows and duplicate completed rows are cleaned automatically, keeping the newest successful configuration. Configuration and messages are stored as ordinary local JSON. Corrupt history is preserved and reported.

History titles now include a readable local timestamp, a short unique record ID, the owner/repo identities for A and B, a short destination branch and status. Selecting a history entry shows both complete branch names and full folder paths beneath the list. Thus repositories with the same basename, or identically named folders on different drives/parents, remain distinguishable. Credentials and query tokens are omitted from displayed repository identities. Existing history entries use this display automatically without changing their saved inputs.

Each exact URL/branch pair has its own private checkout and sync ledger under `%LOCALAPPDATA%\GitSyncTool\<hash>`. Local folder paths do not change that key. `selected-state.json` records source hashes and pending pushes for both basic behaviors. Earlier `state.json` baseline records remain usable as automatic-sync anchors. Existing history files remain readable; restored behavior is determined by whether the message is blank. Keep state to avoid repeated imports. Changing direction or URL spelling uses separate state; equivalent changes across different hashes/directions are not automatically detected.

Do not edit the private checkout or delete state to bypass divergence. Remove a stale `lock` or `history.lock` only after confirming no app instance is active. Inputs are disabled during work and closing waits until the operation finishes.

## Tests

```powershell
python -m unittest -v
```

Tests use temporary local repositories and a test identity; they do not modify global Git settings or contact your real servers.
