import time
from playwright.sync_api import sync_playwright
URL="http://localhost:4195/"
errs=[]
with sync_playwright() as p:
    b=p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required","--use-gl=swiftshader","--enable-webgl","--ignore-gpu-blocklist"])
    pg=b.new_page(viewport={"width":1280,"height":800})
    pg.on("console", lambda m: errs.append(f"{m.type}: {m.text}") if m.type=="error" else None)
    pg.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))
    pg.goto(URL, wait_until="load"); time.sleep(0.5)
    pg.mouse.click(640,400)
    time.sleep(27)   # after boot handoff — corner HUD revealed
    pg.screenshot(path="tools/boot_build/hud_27s.png"); print("shot 27s")
    # check the 4 corners exist and are visible
    st=pg.evaluate("""()=>{const w=document.querySelector('.hud-corners');const cs=[...document.querySelectorAll('.hud-corner')].map(e=>e.className);return{live:w&&w.classList.contains('live'),corners:cs,clock:document.querySelector('.hud-clock')?.textContent};}""")
    print("STATE",st); b.close()
print("ERRORS",len(errs))
for e in errs[:8]: print("  ",e)
