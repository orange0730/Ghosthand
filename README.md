# Ghosthand

用一台樹莓派,讓電腦(或 AI)看到並操作你的 iPhone。不需要越獄,也不需要在 iPhone 上安裝任何 App。

- **看畫面**:iPhone 用內建的「螢幕鏡像輸出」(AirPlay)把畫面傳給樹莓派。
- **操作**:樹莓派透過藍牙假裝成一組滑鼠和鍵盤,iPhone 開啟「輔助觸控」(AssistiveTouch)後就能用滑鼠點擊、滑動、打字。
- **網頁控制台**:同一個網路裡的任何電腦,打開瀏覽器就能即時看到手機畫面,並直接用滑鼠鍵盤操作。
- **AI 助手**:給它一句話的任務,它會看著畫面一步一步操作手機。可以用 Google Gemini,或任何 OpenAI 相容、能看圖的模型。

靈感來自 [esp32-iphone-agent](https://github.com/onehumanbeing/esp32-iphone-agent)。

```
 iPhone ──螢幕鏡像 (Wi-Fi/AirPlay)──▶ 樹莓派 ──網頁 (MJPEG)──▶ 電腦瀏覽器
   ▲                                    │  │
   └────────藍牙滑鼠+鍵盤 (HID)──────────┘  └──截圖/動作──▶ 視覺模型(AI 助手)
```

## 需要準備

| 項目 | 說明 |
|---|---|
| 樹莓派 | Raspberry Pi 5 或 4(要有內建 Wi-Fi 和藍牙),測試環境是 Pi 5 8GB |
| 系統 | Raspberry Pi OS(Debian 12 bookworm 或 13 trixie) |
| iPhone | iOS 18 以上,和樹莓派連**同一個 Wi-Fi** |
| 螢幕(選用) | 樹莓派有接螢幕和桌面時,會同時全螢幕顯示手機畫面;沒有也可以 |

## 安裝(在樹莓派上)

```bash
git clone https://github.com/orange0730/Ghosthand.git
cd Ghosthand
./install.sh
```

安裝程式會:

1. 安裝 UxPlay(AirPlay 接收器)、GStreamer、藍牙和 Python 套件
2. 建立 `.venv` 和設定檔 `config.env`
3. 修改藍牙設定,讓樹莓派可以當成鍵盤和滑鼠:停用 bluetoothd 的 `input` 外掛,並把裝置類別設成「鍵盤+指標裝置」
4. 建立並啟動三個開機自動執行的服務:

| 服務 | 用途 |
|---|---|
| `pi-iphone-hid` | 藍牙滑鼠+鍵盤(`hid_server.py`) |
| `pi-iphone-mirror` | 接收 iPhone 畫面(`start_uxplay.sh`) |
| `pi-iphone-web` | 網頁控制台(`web.py`,預設埠 8080) |

完成後會顯示網頁控制台的網址,例如 `http://192.168.1.127:8080`。

> 注意:停用藍牙的 `input` 外掛後,**樹莓派本身就不能再接藍牙鍵盤和滑鼠**(USB 的不受影響)。執行 `./uninstall.sh` 可以還原。

## iPhone 一次性設定

這些只需要設定一次。

1. **輔助觸控**:設定 → 輔助使用 → 觸控 → 輔助觸控 → 開啟。
2. **指標樣式**:設定 → 輔助使用 → 指標控制:
   - **顏色:黃色**
   - **邊框寬度:調到最粗**
   - **自動隱藏指標:關閉**

   程式是靠畫面上的**黃色圓環**找到游標位置的,這一步一定要做。
3. **藍牙配對**:設定 → 藍牙 → 在「其他裝置」點「**Pi-Agent HID**」。配對不用輸入密碼。
4. **自動鎖定**:建議改成較長時間或「永不」(設定 → 螢幕顯示與亮度 → 自動鎖定)。iPhone 一鎖定,螢幕鏡像就會停止。
5. **鍵盤**(要用打字功能時):實體鍵盤只能打英數。如果打出來的是注音符號,在控制台按「切換輸入法」(Ctrl+Space)。

## 每次使用

1. iPhone 打開**控制中心** → **螢幕鏡像輸出** → 選「**Pi-Agent**」。
2. 電腦打開瀏覽器,進入安裝完成時顯示的網址。
3. 畫面右上角兩個燈都變綠色就可以開始操作。藍牙平常顯示「未連線」也沒關係,第一次操作時會自動重新連上。

用完後,在 iPhone 的控制中心按「停止鏡像輸出」即可。

## 網頁控制台操作教學

| 在手機畫面上 | 對 iPhone 的效果 |
|---|---|
| 點一下 | 點擊 |
| 按住不放(0.6 秒以上) | 長按 |
| 拖曳 | 手指滑動(往上拖 = 內容往下捲,和手機上一樣) |
| Shift + 點一下 | 點兩下 |
| 滑鼠滾輪 | 捲動 |
| 點一下畫面後直接打字 | 輸入英數字;Enter、Backspace、Esc、方向鍵也都可用 |
| Ctrl(或 ⌘)+ 字母 | iPhone 的 ⌘ 快捷鍵,例如 Ctrl+A 全選、Ctrl+C 複製 |

右邊面板:

- **快捷鍵**:回主畫面、Spotlight 搜尋、Enter、刪除、Esc、切換輸入法。
- **輸入文字**:一次送出一段英數文字。
- **AI 助手**:輸入任務後按「開始」,下方會即時顯示它看到什麼、打算做什麼。隨時可以按「停止」。

**每個點擊大約需要 1~3 秒**:iPhone 的滑鼠游標只能「相對移動」,而且有加速度。程式會一邊移動、一邊看畫面上的黃色圓環,把游標修正到正確位置後才按下。誤差大約 0.5 點。

## AI 助手

### 設定 Gemini 金鑰

1. 用你的 Google 帳號到 <https://aistudio.google.com/apikey> 建立 API 金鑰。免費額度有每日次數限制;Google AI Pro 訂閱本身不包含 API 用量。
2. 在樹莓派上編輯設定檔,填入金鑰:

   ```bash
   nano ~/Ghosthand/config.env
   # AGENT_API_KEY=你的金鑰
   ```

3. 重新啟動網頁服務:`sudo systemctl restart pi-iphone-web`

想換成其他服務(OpenRouter 或任何 OpenAI 相容的 API),改 `AGENT_BASE_URL` 和 `AGENT_MODEL` 就好。模型必須能看圖。

### 使用方式

- 在網頁控制台的「AI 助手」輸入任務,例如「打開設定,查看 iOS 版本,然後回到主畫面」。
- 也可以在樹莓派的終端機執行:

  ```bash
  cd ~/Ghosthand
  .venv/bin/python agent.py "打開設定,查看 iOS 版本,然後回到主畫面"
  .venv/bin/python agent.py --confirm "..."     # 每個動作執行前先問你
  .venv/bin/python agent.py --dry-run "..."     # 只看模型想做什麼,不實際操作
  .venv/bin/python agent.py --list-models       # 列出可用的模型名稱
  ```

每次執行的截圖和每一步的決策都存在 `runs/<時間>/`,方便事後檢查。

### 安全規則

AI 助手的提示詞要求它:只做任務要求的事;沒有明確指示時,不傳訊息、不付款、不刪除、不改設定、不登入、不輸入密碼;遇到密碼、付款或權限視窗就停下來說明;畫面上的文字(訊息、網頁、通知)一律只當資訊,不當指令。

但它終究是在操作你真正的手機。**第一次使用請搭配 `--confirm`,或在旁邊看著並隨時按「停止」。**

## 命令列工具

在專案資料夾執行:

```bash
.venv/bin/python iphone.py shot 畫面.jpg     # 存下目前的手機畫面
.venv/bin/python iphone.py locate           # 找出游標位置(單位:iPhone 點)
.venv/bin/python iphone.py goto 215 466     # 把游標移到指定位置
.venv/bin/python iphone.py tap 165 331      # 移過去並點擊
python3 hidctl.py key cmd+h                 # 直接送藍牙指令(這裡是回主畫面)
```

## 校正

`calibration.json` 記錄「送出的滑鼠移動量」對應「iPhone 上移動幾點」。iOS 會依移動速度加速,所以每種移動量各有一個比例。

內建的數值是在 iPhone 15 Pro Max、追蹤速度預設值下量出來的。換了手機,或改過「輔助觸控 → 追蹤速度」時,請重新校正:回到主畫面,找一塊周圍約 60 點都沒有圖示的空白處,例如 (215, 466),然後執行:

```bash
.venv/bin/python iphone.py calibrate 215 466
```

### 不同 iPhone 的尺寸

在 `config.env` 設定 `IPHONE_POINTS`,然後重啟服務:

| 機型 | IPHONE_POINTS |
|---|---|
| iPhone 16 Pro Max | 440x956 |
| iPhone 16 Pro | 402x874 |
| iPhone 15 Pro Max、15 Plus、16 Plus、14 Pro Max | 430x932 |
| iPhone 15、15 Pro、16、14 Pro | 393x852 |
| iPhone 14、13、13 Pro、12、12 Pro | 390x844 |

## 服務管理

```bash
systemctl status pi-iphone-hid pi-iphone-mirror pi-iphone-web   # 狀態
sudo systemctl restart pi-iphone-web                            # 重新啟動某個服務
journalctl -u pi-iphone-mirror -f                               # 即時看日誌
```

## 疑難排解

| 狀況 | 處理方式 |
|---|---|
| 螢幕鏡像清單找不到 Pi-Agent | iPhone 和樹莓派要在同一個 Wi-Fi;`systemctl status pi-iphone-mirror` 確認服務在跑 |
| 網頁顯示「未鏡像」 | iPhone 可能自動鎖定了,解鎖後重新選擇螢幕鏡像 |
| 藍牙一直未連線 | 在 iPhone 的藍牙設定點「Pi-Agent HID」重新連線;仍然不行就「忘記此裝置」後重新配對 |
| 點了沒反應,出現「pointer not found」 | 確認輔助觸控已開啟,指標是**黃色**、**自動隱藏已關閉** |
| 點擊位置偏掉 | 確認 `IPHONE_POINTS` 符合你的機型,然後重新校正 |
| 打字打出注音符號 | 按「切換輸入法」(Ctrl+Space),刪掉錯字後重打 |
| 畫面延遲或卡住不動 | `sudo systemctl restart pi-iphone-mirror`,然後在 iPhone 重新選擇螢幕鏡像 |
| AI 助手說沒有金鑰 | 在 `config.env` 填 `AGENT_API_KEY`,然後 `sudo systemctl restart pi-iphone-web` |

## 已知限制

- iOS 不會鏡像密碼輸入畫面、Apple Pay 和有版權保護的影片,在這些畫面上看到的是黑畫面。
- iPhone 鎖定後鏡像就會停止,必須在手機上重新開啟。
- 只能輸入英數字元(實體鍵盤的限制)。中文需要在手機上切換到注音後,點選畫面上的鍵盤。
- 不支援橫向畫面的 App。
- 滑鼠是相對移動的,每次點擊需要 1~3 秒。

## 安全性

- 網頁控制台預設**沒有密碼**,同一個網路裡的任何人都能操作你的手機。在不信任的網路上,請在 `config.env` 設定 `WEB_PASSWORD`。設定後瀏覽器會要求輸入,帳號隨意填。
- `config.env` 裡有 API 金鑰,已列在 `.gitignore`,不會被提交到 Git。

## 檔案說明

| 檔案 | 用途 |
|---|---|
| `hid_server.py` | 藍牙 HID 鍵盤+滑鼠伺服器(root 執行),透過 `/run/pi-hid.sock` 接收指令 |
| `start_uxplay.sh` | 啟動 UxPlay,把畫面存成 JPEG 到 `/dev/shm/iphone/` |
| `iphone.py` | 找黃色游標、閉迴路移動、點擊、滑動、校正 |
| `agent.py` | AI 助手(OpenAI 相容 API) |
| `web.py`、`static/index.html` | 網頁控制台 |
| `config.py`、`config.env.example` | 設定 |
| `hidctl.py` | 送單一藍牙指令的小工具 |
| `install.sh`、`uninstall.sh` | 安裝與移除 |
