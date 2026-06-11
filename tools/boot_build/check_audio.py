import time
from playwright.sync_api import sync_playwright

URL = "http://localhost:4191/"
errors = []
with sync_playwright() as p:
    b = p.chromium.launch(args=[
        "--autoplay-policy=no-user-gesture-required",
        "--use-gl=swiftshader", "--enable-webgl", "--ignore-gpu-blocklist"])
    pg = b.new_page(viewport={"width": 1100, "height": 700})
    pg.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type == "error" else None)
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    pg.goto(URL, wait_until="load")
    time.sleep(0.4)
    pg.click("button[data-lang='fr']")
    pg.mouse.click(550, 350)

    def snap(label):
        st = pg.evaluate("""() => {
            const m = document.getElementById('boot-music');
            const v = document.getElementById('boot-audio');
            return { mSrc: m && m.getAttribute('src'), mPaused: m && m.paused,
                     mVol: m && +m.volume.toFixed(3), mLoop: m && m.loop, mTime: m && +m.currentTime.toFixed(1),
                     vSrc: v && v.getAttribute('src'), vPaused: v && v.paused, vTime: v && +v.currentTime.toFixed(1) };
        }""")
        print(f"@{label}:", st)

    time.sleep(6);  snap("6s")    # boot in progress
    time.sleep(18); snap("24s")   # after handoff (>22.5s): music faint, looping
    time.sleep(6);  snap("30s")   # past first 28s loop: music must still play
    b.close()

print("CONSOLE ERRORS:", len(errors))
for e in errors[:15]:
    print("  ", e)
