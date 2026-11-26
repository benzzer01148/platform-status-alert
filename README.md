# แจ้งเตือน LINE API / Meta ล่ม ไปที่ LINE และ Google Chat

ระบบนี้เช็กสถานะทุก 5 นาทีด้วย GitHub Actions แล้วส่งข้อความไปที่ **LINE (Messaging API)** และ/หรือ **Google Chat space** เมื่อ

| แหล่ง | อ่านจาก | แจ้งเมื่อ |
|---|---|---|
| LINE API Status (`monitor.py`) | JSON API ของ Statuspage → ถ้าใช้ไม่ได้ใช้ Atom (`history.atom`) → RSS (`history.rss`) | incident ใหม่และทุกอัปเดต (Investigating → Identified → Monitoring → Resolved) และ (เฉพาะ JSON) สถานะของ Messaging API, Webhook, Login, LIFF เปลี่ยน |
| Meta Messenger Platform (`meta_monitor.py`) | https://metastatus.com/outage-events-feed-messenger.rss | มีรายการปัญหาใหม่ / อัปเดต / รายการหายไป (กลับมาปกติ) |
| Meta Messenger API for Instagram | https://metastatus.com/outage-events-feed-ig-messenger.rss | เหมือนกัน |

ฟีด Meta ตอนนี้ว่าง (ไม่มีปัญหา) ผมจึงยังไม่เคยเห็นหน้าตารายการจริง สคริปต์อ่านแบบทั่วไป (title/description/pubDate/guid) และเดาว่ารายการจะหายไปเมื่อแก้เสร็จ

เลือกแหล่งของ LINE เองได้ด้วย env `STATUS_SOURCE` = `auto` (ค่าเริ่มต้น) / `json` / `atom` / `rss` ข้อต่างคือ Atom/RSS มีเฉพาะ incident ที่ประกาศ ส่วน JSON เห็นสถานะของแต่ละบริการด้วย

กันส่งซ้ำ: เรื่องที่ส่งแล้วจะจำไว้ใน `state.json` และ `meta_state.json` และจำแยกตามช่องทาง ถ้า LINE ล่ม (ส่งไม่ได้) Google Chat จะได้รับครั้งเดียว ส่วน LINE จะส่งตามหลังเมื่อกลับมา

## Secrets ที่ต้องใส่ (อย่าส่งในแชต อย่าใส่ในโค้ด)

ใส่ที่ repo → **Settings → Secrets and variables → Actions → New repository secret**

| ชื่อ secret | ค่า | จำเป็นไหม |
|---|---|---|
| `GOOGLE_CHAT_WEBHOOK_URL` | URL webhook ของ Google Chat space (ทั้งเส้น รวม `key=` และ `token=`) | ใส่อย่างน้อย 1 ช่องทาง |
| `LINE_CHANNEL_ACCESS_TOKEN` | Channel access token (long-lived) จาก https://developers.line.biz/console/ → channel → แท็บ *Messaging API* → Issue | ใส่อย่างน้อย 1 ช่องทาง |
| `LINE_TO` | ID ผู้รับ: user ID ของตัวเอง (แท็บ *Basic settings* → *Your user ID*, ขึ้นต้น `U`, ต้องแอดบอทเป็นเพื่อนก่อน) หรือ group ID (ขึ้นต้น `C`) หลายค่าคั่นด้วย `,` | ไม่บังคับ ถ้าไม่ใส่จะ broadcast ให้เพื่อนบอททุกคน |

ใส่แค่ Google Chat อย่างเดียวก็ใช้ได้ ใส่ LINE เพิ่มทีหลังได้

## ติดตั้ง

1. สร้าง GitHub repo แล้วอัปโหลดทุกไฟล์ในโฟลเดอร์นี้ รวม `.github/workflows/line-status.yml`
   - แนะนำ **public repo**: GitHub Actions ฟรีไม่จำกัดนาที ส่วน private repo ฟรีแค่ 2,000 นาที/เดือน แต่รันทุก 5 นาทีใช้ราว 8,600 นาที/เดือน (ใน repo ไม่มีความลับ เพราะ token อยู่ใน Secrets)
   - ถ้าต้องเป็น private ให้เปลี่ยน cron เป็น `*/30 * * * *` หรือห่างกว่านั้น
2. ใส่ Secrets ตามตารางด้านบน
3. แท็บ **Actions** → *Status alerts (LINE API + Meta)* → **Run workflow** รอบแรก
   - รอบแรกจะ **ไม่** ส่ง incident เก่าที่ปิดแล้ว จะส่งเฉพาะเรื่องที่ยังไม่จบ

## ทดสอบบนเครื่อง

```bash
DRY_RUN=1 python monitor.py                      # พิมพ์ข้อความออกจอ ไม่ส่งจริง
DRY_RUN=1 python meta_monitor.py
python -I tests/test_monitor.py                  # เทสต์จำลอง ไม่ใช้เน็ต
python -I tests/test_meta.py                     # เทสต์จำลอง Meta
```

## ข้อจำกัด

- ถ้า Messaging API ล่มเอง ข้อความ LINE จะไปไม่ถึงจนกว่าจะกลับมา Google Chat จึงเป็นช่องทางสำรองที่ดี
- GitHub Actions cron อาจดีเลย์ 5–15 นาทีช่วงคนใช้เยอะ
- repo public ที่ไม่มี commit 60 วัน GitHub จะปิด schedule อัตโนมัติ ต้องกด Enable กลับ
- push message ของ LINE นับโควตาข้อความรายเดือนของ OA แต่ incident เกิดไม่บ่อย
- ถ้าอ่านฟีดไม่ได้ สคริปต์จะไม่ส่งข้อความผิด ๆ แต่ run จะเป็นสีแดงในแท็บ Actions
