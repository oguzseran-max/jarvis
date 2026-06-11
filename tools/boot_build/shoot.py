import time
from playwright.sync_api import sync_playwright

URL = "http://localhost:4189/"
SHOTS = [1.5, 4.0, 8.0, 13.0, 19.0, 24.0]
errors = []

with sync_playwright() as p:
    browser = p.chromium.launch(args=[
        "--autoplay-policy=no-user-gesture-required",
        "--use-gl=swiftshader", "--enable-webgl", "--ignore-gpu-blocklist",
    ])
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.goto(URL, wait_until="load")
    time.sleep(0.5)
    # pick French, then click to start the boot (user gesture)
    page.click("button[data-lang='fr']")
    page.mouse.click(640, 400)
    t0 = time.time()
    for ts in SHOTS:
        dt = ts - (time.time() - t0)
        if dt > 0:
            time.sleep(dt)
        page.screenshot(path=f"tools/boot_build/shot_{ts:04.1f}.png")
        print(f"shot @ {ts:.1f}s")
    g = page.eval_on_selector(".boot-greet", "e => e.textContent") if page.query_selector(".boot-greet") else None
    s = page.eval_on_selector(".boot-sub", "e => e.textContent") if page.query_selector(".boot-sub") else None
    print("GREET:", g)
    print("SUB:", s)
    browser.close()

print("CONSOLE ERRORS:", len(errors))
for e in errors[:20]:
    print("  ", e)
