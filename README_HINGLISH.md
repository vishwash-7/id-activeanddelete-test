# ID Inactive & Deleted — Setup Guide (Hinglish)

Ye Telegram group bot group ke andar members ki **message activity** track karta hai. Ye Telegram ka actual online/last-seen status read nahi karta.

## Bot kya karega?

- Private chat mein `/start` par logo, welcome message aur **➕ Add to Group** button dikhayega. Group mein `/start` bilkul silent rahega—na reply, na side effect.
- Group mein admin banne ke baad automatic kaam karega; inactivity setup ke liye koi group command nahi chahiye.
- Group member 13 din tak koi message na bheje to warning bhejega.
- Warning ke baad 24 ghante ka grace period dega.
- Deadline tak us member ka naya group message na aaye to bot use ban karega aur reason `Inactive for too long` post karega.
- Agar aap/admin inactivity-ban ke baad member ko unban/reinstate karta hai, purana warning aur ban state fresh event se reset hota hai. Naya 13-day timer uske group mein wapas aane se start hota hai; purane scan ka result nayi state ko overwrite karke turant re-ban nahi karna chahiye.
- Warning ya ban ke dauran naya group message aa jaaye toh stale warning/ban claim cancel ho jata hai.
- Group ke admin/owner ko inactivity ban se exempt rakhta hai.
- Activity aur access settings SQLite database mein save hoti hain, isliye restart ke baad data rehta hai.
- `/setinactive`, `/setwarning`, aur `/inactive` sirf GC mein kaam karte hain. Har GC ka apna alag inactivity threshold aur warning time hota hai; ek GC mein badlaav doosre GC ko affect nahi karta. In commands ko us GC ke Telegram admins, approved Bot Admin, ya bot owner use kar sakte hain. Private bot chat mein in commands par koi reply nahi aayega.

## Bot access commands (sirf bot ki private chat mein)

### Pehli baar owner setup

1. Apna numeric Telegram user ID kisi ID lookup bot se lein.
2. `.env.example` ki copy bana kar `.env` naam dein.
3. `.env` mein `BOT_TOKEN=` ke baad `@BotFather` ka token daalein. Is project mein owner ID `6195715245` default hai; chahein toh `OWNER_IDS=6195715245` explicitly rakhein. Multiple owners ke liye comma use karein.
4. Render par deploy kar rahe hain to **Environment** mein `BOT_TOKEN` zaroor set karein. `OWNER_IDS` ko `6195715245` par set rakhein (code mein bhi yahi fallback configured hai), taaki `/lockall` aur `/approve` owner account se kaam karein.

### `/lockall`

- `/lockall` ya `/lockall on` — access lock enable karta hai.
- `/lockall off` — access lock disable karta hai.
- Lock ON hone par sirf approved users (`Bot Use Only` ya `Bot Admin`) bot ko groups mein add kar sakte hain. Unapproved user add karega to bot **“Aapke paas permission nahi hai”** message dekar group leave karega.
- `/lockall` aur `/approve` sirf bot owner (`OWNER_IDS`) ya approved Bot Admin role use kar sakte hain. Group mein in commands ko bhejne par bot bilkul silent rahega—na reply, na access change. Group ke normal Telegram admins aur `Bot Use Only` role ko in dono commands ka access automatically nahi milta.

### `/approve USER_ID` ya `/approve @username`

Command target ke liye do inline options dikhayega:

- **Sirf Bot Use Karne Dein** — user lockall ke waqt bot ko group mein add/use kar sakta hai, lekin bot access manage nahi kar sakta.
- **Bot Admin Banayein** — user bot ko groups mein add kar sakta hai aur `/lockall` / `/approve` use kar sakta hai. Security ke liye naye Bot Admin ka role sirf owner grant kar sakta hai.

`@username` se approval tabhi kaam karega jab bot us account ko pehle `/start`, group message ya membership event se dekh chuka ho. User ID sabse reliable tareeqa hai. Bot mein role approval aur Telegram group ka actual administrator role alag cheezein hain.

## Inactivity settings commands (sirf GC; group admins, approved Bot Admin ya bot owner)

Ye commands usi GC mein bhejein jiska timer badalna hai. Private bot chat mein in commands ko bhejne par bot **koi reply nahi dega**. Group settings SQLite database mein `chat_id` ke hisaab se save hoti hain, isliye har GC ka configuration alag aur restart ke baad persistent rahega.

- `/inactive` — jis GC mein command bheji, usi ki activity limit aur warning grace-period dekhein.
- `/setinactive 20` — sirf is GC ki inactivity limit 20 din karein. Allowed range: 1–365 din.
- `/setwarning 48` — sirf is GC mein warning ke baad 48 ghante dein. Allowed range: 1–720 ghante.

**Command boundaries:**

- `/start` sirf private bot chat mein welcome/logo/Add to Group button dikhata hai. GC ya channel mein `/start` ko completely ignore kiya jata hai.
- Inactivity settings commands sirf group/supergroup mein kaam karti hain. Command chalane wala us GC ka Telegram admin/owner, approved `Bot Admin`, ya bot owner ho. Bot Admin command current GC ki settings hi badalta hai; `Bot Use Only` role settings change nahi kar sakta.
- Agar aapne Group A mein `/setinactive 20` kiya, toh Group B ka timer bilkul nahi badlega. Har group apni current settings `/inactive` se dekh sakta hai.

**Behavior details:**

- `/setinactive` kisi member ka last-activity timestamp reset nahi karta. Sirf current group ke existing warned members ko fresh grace period milta hai; naye eligible members ko pehle warning di jaati hai, seedha ban nahi.
- Activity limit badhane se koi warning threshold ke liye abhi due nahi rehti, toh scanner us stale warning ko clear kar deta hai. Member jab updated threshold meet karega, tab warning dobara milegi.
- `/setwarning` current group ke existing warnings ka countdown command ke waqt se fresh full period par reset karta hai. Doosre groups ke warning clocks nahi badalte.
- New group defaults environment variables `ACTIVITY_DAYS` and `WARNING_HOURS` se aate hain (default 13 days / 24 hours). Har GC apni settings change kar sakta hai; purani global private-chat activity settings use nahi hoti.

## Important limitations — zaroor padhein

1. **Group inactivity ≠ Telegram offline status.** Bot sirf isi group mein aayi messages (text, media, stickers, etc.) ko activity maanta hai. Member Telegram par active ho sakta hai, lekin agar wo is group mein message nahi karta to bot usse inactive maan sakta hai.
2. **Purane existing members:** Telegram bots group ki complete member list download nahi kar sakte. Bot un members ko track karna shuru karega jinke messages ya join events bot add hone ke baad receive honge. Bot add karne se pehle ke 13 din ka history scan nahi kar sakta.
3. **Deleted accounts:** Official Telegram Bot API deleted-account flag ya complete member-list enumeration expose nahi karti. Is Bot-API-only version mein deleted account ko identify karke exactly 30 din baad remove karna implement nahi hai.
4. Bot ko group mein **administrator** banaiye aur **Ban Users / Restrict Members** permission enable karein. Full message activity receive karne ke liye bot ko admin hi rakhein.
5. Settings test karne ke liye jis GC mein test kar rahe hain, usi GC mein admin/Bot Admin account se `/setinactive 1` aur `/setwarning 1` bhejein; production ke liye usi GC mein `/setinactive 13` aur `/setwarning 24` set karein. Private bot chat mein ye teen settings commands jaanbujhkar silent hain. Scan interval `CHECK_INTERVAL_MINUTES` environment variable se control hota hai (e.g. 5 minutes).
6. Telegram API errors, missing admin rights, hosting/database issues ya wrong permissions action ko rok sakte hain. Hosting logs check karein.

## Windows par run karna

1. Python 3.11 ya newer install karein.
2. `.env.example` ki copy bana kar `.env` naam dein aur `BOT_TOKEN` + `OWNER_IDS` set karein.
3. Terminal/CMD ko project folder mein kholkar chalayein:

```powershell
py -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
python main.py
```

4. `Starting ID Inactive & Deleted as @...` log aaye to bot start ho gaya. Terminal band karne par local bot stop ho jayega.

## GitHub + Render

- `.env` ya bot token ko GitHub par kabhi commit mat karein.
- Project ko GitHub repo mein upload karein.
- Render par Background Worker banaiye aur build command `pip install -r requirements.txt`, start command `python main.py` set karein.
- Environment variables mein `BOT_TOKEN` aur `OWNER_IDS` add karein; `DB_PATH=/var/data/id_inactive_deleted.sqlite3` set karein aur `/var/data` par persistent disk attach karein.
- `render.yaml` mein example configuration included hai; Render plan/pricing account ke hisaab se vary kar sakte hain.
