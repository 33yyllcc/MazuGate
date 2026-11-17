<div align="center">

# 🛡️ MazuGate

**Telegram MTProto Proxy Manager • مدير بروكسيات تلكرام**

Fast proxy engine + full Telegram control bot + multi-server load balancing
محرك بروكسي سريع + بوت تحكم كامل بالتلكرام + توزيع على أكثر من سيرفر

![Ubuntu](https://img.shields.io/badge/Ubuntu-22.04%20%7C%2024.04-E95420?logo=ubuntu&logoColor=white)
![Engine](https://img.shields.io/badge/engine-telemt%20(Rust)-orange)
![Python](https://img.shields.io/badge/bot-Python%20stdlib-3776AB?logo=python&logoColor=white)
![Languages](https://img.shields.io/badge/lang-العربية%20%7C%20English-success)

**By Mazu • Programmed by [t.me/ozozz](https://t.me/ozozz)**

[العربية](#-العربية) • [English](#-english)

</div>

---

## 🇮🇶 العربية

### ⚡ التنصيب بأمر واحد

على سيرفر **Ubuntu 24** فارغ:

```bash
curl -fsSL https://raw.githubusercontent.com/33yyllcc/MazuGate/main/install.sh -o install.sh && sudo bash install.sh
```

يسألك أول شي عن **اللغة**، وبعدها:
1. **توكن البوت** من [@BotFather](https://t.me/BotFather).
2. **حسابك**: تفتح البوت وتدوس `/start`، والتنصيب يعرف الـ ID مالك وحده.
3. **البورت** (افتراضي 443).
4. **تاك القناة الإعلانية** (اختياري، تگدر تربطه بعدين من البوت).

أول ما يخلص، البوت يدزلك الروابط وشرح ربط القناة الإعلانية.

### ✨ المميزات

| | |
| --- | --- |
| 🚀 **محرك سريع** | [telemt](https://github.com/telemt/telemt) مكتوب بـ Rust، يستعمل كل الكورات وخفيف عالرام |
| 📢 **قناة إعلانية** | تربط التاك من البوت بدون ريستارت، ويطبق على كل الروابط وكل السيرفرات |
| 📊 **إحصائيات** | المتصلين، التحميل والرفع، وبلوك لكل سيرفر: الكارت ونسبته، المعالج، الرام |
| ⏱ **تقارير تلقائية** | كل 6 ساعات وتقرير يومي نص الليل |
| 🖧 **أكثر من سيرفر** | رابط واحد للمشتركين، والدومين يوزعهم على كل السيرفرات |
| ☁️ **كلاودفلير** | البوت يضيف السيرفر للدومين لما يجهز، ويشيله إذا وگف |
| 🩺 **تشخيص** | يفحص كل سيرفر: الضغط، طريق تلكرام، ضياع الشبكة، والتهيئة |
| ⚡ **تهيئة للضغط العالي** | الكيرنل، كارت الشبكة، الحدود، وإعدادات البروكسي لعشرات الآلاف من المتصلين |
| 👮 **أدمنية** | كل الأدمنية بنفس صلاحيات المالك |
| 🌐 **لغتين** | كل مستخدم يختار عربي أو إنكليزي بأول دخول |

### 🤖 البوت

| الزر | شيسوي |
| --- | --- |
| 📊 الإحصائيات | المجموع لكل السيرفرات، وبعده بلوك مرتب لكل سيرفر |
| 🔗 الروابط | روابط جاهزة للضغط |
| 🖧 السيرفرات | إضافة وحذف سيرفرات، الحالة، التهيئة، وأمر التحديث |
| 🩺 تشخيص | فحص كل السيرفرات بنفس الوكت |
| 📢 القناة الإعلانية | ربط التاك وشرح @MTProxybot |
| ➕ / 🗑 | إنشاء وحذف روابط (تنتقل لكل السيرفرات) |
| 👮 الأدمنية | رفع وتنزيل أدمنية |
| ⚙️ الإعدادات | مدة التقرير، سرعة البورت، دومين الروابط، اللغة |

### 🖧 أكثر من سيرفر برابط واحد

1. اربط دومين من **⚙️ الإعدادات ← 🌍 دومين الروابط**. الدومين يأشر على السيرفر، وإذا بكلاودفلير لازم الغيمة **رمادية ☁️**.
2. **🖧 السيرفرات ← ➕ إضافة سيرفر**، ارسل IP السيرفر الجديد.
3. شغل الأمر اللي يعطيك اياه البوت على السيرفر الجديد.
4. البوت ينقله الروابط والتاك ويتأكد إنها مطابقة. ما يدخل بالتوزيع إلا لما يجهز تماماً.
5. اربط كلاودفلير (اختياري) حتى البوت يدير سجلات الدومين وحده.

### 🔄 التحديث

على كل سيرفر (ما يغير الروابط ولا التاك):

```bash
curl -fsSL https://raw.githubusercontent.com/33yyllcc/MazuGate/main/update.sh -o update.sh && sudo bash update.sh
```

### 🗑 الحذف

```bash
curl -fsSL https://raw.githubusercontent.com/33yyllcc/MazuGate/main/uninstall.sh -o uninstall.sh && sudo bash uninstall.sh all
```

### 💻 المتطلبات

- **النظام**: Ubuntu 22.04 أو 24.04 (يفضل 24)، وصلاحية root.
- **لعشرات الآلاف من المتصلين**: 4 كور (ويا AES-NI) + 8GB رام + بورت 1Gbps أو أكثر.

---

## 🇬🇧 English

### ⚡ One-command install

On a fresh **Ubuntu 24** server:

```bash
curl -fsSL https://raw.githubusercontent.com/33yyllcc/MazuGate/main/install.sh -o install.sh && sudo bash install.sh
```

It first asks for the **language**, then:
1. **Bot token** from [@BotFather](https://t.me/BotFather).
2. **Your account**: open the bot and press `/start`; the installer detects your ID.
3. **Port** (default 443).
4. **Sponsored-channel tag** (optional, you can link it later from the bot).

When it finishes, the bot sends you the links and the sponsored-channel guide.

### ✨ Features

| | |
| --- | --- |
| 🚀 **Fast engine** | [telemt](https://github.com/telemt/telemt), written in Rust, uses every core with a small memory footprint |
| 📢 **Sponsored channel** | Link the tag from the bot without a restart; it applies to every link and every server |
| 📊 **Statistics** | Connections, download/upload, and a block per server: network and %, CPU, RAM |
| ⏱ **Automatic reports** | Every 6 hours plus a daily report at midnight |
| 🖧 **Multi-server** | One link for users; the domain spreads them over all servers |
| ☁️ **Cloudflare** | The bot adds a server to the domain once ready and removes it if it goes down |
| 🩺 **Diagnostics** | Checks every server: pressure, Telegram path, packet loss, tuning |
| ⚡ **High-load tuning** | Kernel, network card, limits and proxy settings for tens of thousands of users |
| 👮 **Admins** | Every admin has the owner's full permissions |
| 🌐 **Two languages** | Each user picks Arabic or English on first use |

### 🤖 The bot

| Button | What it does |
| --- | --- |
| 📊 Stats | Totals for all servers, then a tidy block per server |
| 🔗 Links | Tap-to-connect proxy links |
| 🖧 Servers | Add/remove servers, status, tuning and the update command |
| 🩺 Diagnostics | Checks every server at once |
| 📢 Sponsored channel | Link the tag, with an @MTProxybot guide |
| ➕ / 🗑 | Create/delete links (copied to every server) |
| 👮 Admins | Add/remove admins |
| ⚙️ Settings | Report interval, port speed, link domain, language |

### 🖧 Several servers, one link

1. Link a domain in **⚙️ Settings → 🌍 Link domain**. It must point to the server, and on Cloudflare the cloud must be **grey ☁️**.
2. **🖧 Servers → ➕ Add server**, then send the new server's IP.
3. Run the command the bot gives you on the new server.
4. The bot copies links and tag and verifies them. The server only gets users once it is fully ready.
5. Optionally link Cloudflare so the bot manages the domain records for you.

### 🔄 Update

On every server (keeps links and tag):

```bash
curl -fsSL https://raw.githubusercontent.com/33yyllcc/MazuGate/main/update.sh -o update.sh && sudo bash update.sh
```

### 🗑 Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/33yyllcc/MazuGate/main/uninstall.sh -o uninstall.sh && sudo bash uninstall.sh all
```

### 💻 Requirements

- **OS**: Ubuntu 22.04 or 24.04 (24 recommended), with root access.
- **For tens of thousands of users**: 4 cores (with AES-NI) + 8GB RAM + a 1Gbps or faster port.

---

<div align="center">

### 🗂 Files

`install.sh` installer • `update.sh` update • `uninstall.sh` uninstall • `server-tune.sh` high-load tuning
`telemt-config.py` proxy settings • `bot/telemtg_bot.py` bot + node agent

**MazuGate** • by **Mazu** • Programmed by [**t.me/ozozz**](https://t.me/ozozz)

Proxy engine: [telemt](https://github.com/telemt/telemt) (by its authors, under its own license)

</div>
