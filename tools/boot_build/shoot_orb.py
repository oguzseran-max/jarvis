import time
from playwright.sync_api import sync_playwright

URL = "http://localhost:4192/"
SHOTS = [21.0, 23.5, 26.0, 33.0]
errors = []
with sync_playwright() as p:
    b = p.chromium.launch(args=[
        "--autoplay-policy=no-user-gesture-required",
        "--use-gl=swiftshader", "--enable-webgl", "--ignore-gpu-blocklist"])
    pg = b.new_page(viewport={"width": 1280, "height": 800})
    pg.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type == "error" else None)
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    pg.goto(URL, wait_until="load")
    time.sleep(0.4)
    pg.mouse.click(640, 400)
    t0 = time.time()
    for ts in SHOTS:
        dt = ts - (time.time() - t0)
        if dt > 0:
            time.sleep(dt)
        pg.screenshot(path=f"tools/boot_build/orb_{ts:04.1f}.png")
        print(f"shot @ {ts:.1f}s")
    b.close()
print("CONSOLE ERRORS:", len(errors))
for e in errors[:15]:
    print("  ", e)
