"""Automated UI screenshot tests — portrait, landscape, desktop."""
import asyncio, os, ssl
from playwright.async_api import async_playwright

BASE  = "https://localhost:8080"
CREDS = {"username": "alex", "password": "kareN!msp1"}
OUT  = "/tmp/ui_screenshots"
os.makedirs(OUT, exist_ok=True)

VIEWPORTS = [
    ("portrait",   {"width": 390,  "height": 844}),
    ("landscape",  {"width": 844,  "height": 390}),
    ("desktop",    {"width": 1280, "height": 800}),
]

PAGES = [
    ("dashboard", "/"),
    ("template",  "/template"),
    ("preview",   "/preview"),
    ("smime",     "/smime"),
    ("settings",  "/settings"),
    ("log",       "/log"),
    ("setup",     "/setup"),
]

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--ignore-certificate-errors"])
        issues = []
        for vp_name, vp in VIEWPORTS:
            ctx = await browser.new_context(
                viewport=vp,
                ignore_https_errors=True,
                http_credentials=CREDS,
            )
            page = await ctx.new_page()
            for pg_name, path in PAGES:
                await page.goto(BASE + path, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(600)

                fname = f"{OUT}/{vp_name}_{pg_name}.png"
                await page.screenshot(path=fname, full_page=False)  # viewport only (what user sees)

                # Check nav visibility
                nav = page.locator("nav")
                nav_box = await nav.bounding_box()
                if nav_box is None or nav_box["y"] > 10:
                    issues.append(f"NAV HIDDEN  {vp_name}/{pg_name}  box={nav_box}")
                elif nav_box["y"] < 0:
                    issues.append(f"NAV OFFSCREEN {vp_name}/{pg_name}  y={nav_box['y']:.0f}")

                # Check for horizontal overflow (body wider than viewport)
                body_w = await page.evaluate("document.body.scrollWidth")
                if body_w > vp["width"] + 2:
                    issues.append(f"OVERFLOW    {vp_name}/{pg_name}  body={body_w}px > vp={vp['width']}px")

                print(f"  [{vp_name:9}] {pg_name:10}  nav_y={nav_box['y'] if nav_box else '?':>4}  body_w={body_w}")

            await ctx.close()
        await browser.close()

        print("\n── Issues ──────────────────────────────────────")
        if issues:
            for i in issues:
                print(" !", i)
        else:
            print("  None found.")
        print(f"\nScreenshots saved to {OUT}/")

asyncio.run(run())
