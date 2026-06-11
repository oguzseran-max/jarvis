import time
from playwright.sync_api import sync_playwright
URL="http://localhost:4194/"
SHOTS=[20.7, 22.8, 23.8, 26.5]
errs=[]
with sync_playwright() as p:
    b=p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required","--use-gl=swiftshader","--enable-webgl","--ignore-gpu-blocklist"])
    pg=b.new_page(viewport={"width":1180,"height":740})
    pg.on("console", lambda m: errs.append(f"{m.type}: {m.text}") if m.type=="error" else None)
    pg.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))
    pg.goto(URL, wait_until="load"); time.sleep(0.5)
    pg.mouse.click(590,360); t0=time.time()
    for ts in SHOTS:
        dt=ts-(time.time()-t0)
        if dt>0: time.sleep(dt)
        pg.screenshot(path=f"tools/boot_build/ho_{ts:04.1f}.png"); print(f"shot {ts}")
    st=pg.evaluate("""()=>{const m=document.getElementById('boot-music');const o=document.getElementById('boot-overlay');return{paused:m&&m.paused,vol:m&&+m.volume.toFixed(3),overlayOpacity:o&&getComputedStyle(o).opacity, done:o&&o.classList.contains('done')};}""")
    print("STATE",st); b.close()
print("ERRORS",len(errs))
for e in errs[:8]: print("  ",e)
