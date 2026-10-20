"""Скріншоти для docs/flow.md зі стрілками.

Проходить шлях нового ноутбука на тимчасовій копії репозиторію: майстер першого
налаштування → панель НРК, і знімає кожен крок у браузері зі стрілками поверх
сторінки. Справжні inventory/ і secrets/ не чіпаються.

Справжнього сервера й WireGuard тут немає, тому SSH, розгортання і встановлення
тунелю ІМІТУЮТЬСЯ (див. simulate_setup): у документації видно інтерфейс, а не
результат роботи з сервером.

Запуск:  .venv\\Scripts\\python docs\\tools\\screens.py
Потрібно: pip install playwright (браузер не завантажується — береться встановлений Edge).
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from playwright.sync_api import Page, sync_playwright  # noqa: E402

from fleet import bootstrap  # noqa: E402
from fleet.ui import UiServer  # noqa: E402

OUT = REPO / "docs" / "img"
VIEWPORT = {"width": 1440, "height": 1240}
SERVER, PILOT, SERIAL = "198.51.100.20", "ivan", "FLU-2026-0001"

# Малює рамку навколо елемента, підпис і стрілку від підпису до рамки
OVERLAY_JS = """
(items) => {
  document.querySelectorAll('.anno').forEach((e) => e.remove());
  const NS = 'http://www.w3.org/2000/svg', COLOR = '#ff4d1a', PAD = 5, GAP = 64;
  const svg = document.createElementNS(NS, 'svg');
  svg.classList.add('anno');
  Object.assign(svg.style, { position: 'fixed', left: 0, top: 0, width: '100vw', height: '100vh', pointerEvents: 'none', zIndex: 9998 });
  svg.innerHTML = `<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
    <path d="M0,0 L10,5 L0,10 z" fill="${COLOR}"/></marker></defs>`;
  document.body.appendChild(svg);
  for (const it of items) {
    const r = it.box;
    const box = { x: r.x - PAD, y: r.y - PAD, w: r.width + 2 * PAD, h: r.height + 2 * PAD };
    const rect = document.createElementNS(NS, 'rect');
    Object.entries({ x: box.x, y: box.y, width: box.w, height: box.h, rx: 8, fill: 'none', stroke: COLOR, 'stroke-width': 3 })
      .forEach(([k, v]) => rect.setAttribute(k, v));
    svg.appendChild(rect);

    const label = document.createElement('div');
    label.className = 'anno';
    label.innerHTML = `<b style="display:inline-grid;place-items:center;width:22px;height:22px;border-radius:50%;background:#fff;color:${COLOR};margin-right:8px;font-size:13px;flex:none">${it.num}</b>`;
    label.append(it.text);
    Object.assign(label.style, {
      position: 'fixed', zIndex: 9999, background: COLOR, color: '#fff', font: '600 14px/1.35 system-ui, Segoe UI, sans-serif',
      padding: '7px 12px 7px 8px', borderRadius: '9px', maxWidth: '300px', boxShadow: '0 4px 14px rgba(0,0,0,.25)',
      display: 'flex', alignItems: 'center',
    });
    document.body.appendChild(label);
    const lw = label.offsetWidth, lh = label.offsetHeight;
    let lx, ly, from, to;
    const cx = box.x + box.w / 2, cy = box.y + box.h / 2;
    const dy = it.dy || 0;
    if (it.side === 'left' && it.outer) { lx = it.outer.x - 28 - lw; ly = cy - lh / 2 + dy; }
    else if (it.side === 'right' && it.outer) { lx = it.outer.x + it.outer.width + 28; ly = cy - lh / 2 + dy; }
    else if (it.side === 'left') { lx = box.x - GAP - lw; ly = cy - lh / 2 + dy; }
    else if (it.side === 'right') { lx = box.x + box.w + GAP; ly = cy - lh / 2 + dy; }
    else if (it.side === 'top') { lx = cx - lw / 2 + (it.shift || 0); ly = box.y - GAP - lh; }
    else { lx = cx - lw / 2 + (it.shift || 0); ly = box.y + box.h + GAP; }
    lx = Math.max(8, Math.min(lx, innerWidth - lw - 8));
    ly = Math.max(8, Math.min(ly, innerHeight - lh - 8));
    label.style.left = lx + 'px';
    label.style.top = ly + 'px';
    if (it.side === 'left') { from = [lx + lw, ly + lh / 2]; to = [box.x, cy]; }
    else if (it.side === 'right') { from = [lx, ly + lh / 2]; to = [box.x + box.w, cy]; }
    else if (it.side === 'top') { from = [lx + lw / 2, ly + lh]; to = [cx, box.y]; }
    else { from = [lx + lw / 2, ly]; to = [cx, box.y + box.h]; }
    const line = document.createElementNS(NS, 'line');
    Object.entries({ x1: from[0], y1: from[1], x2: to[0], y2: to[1], stroke: COLOR, 'stroke-width': 3, 'marker-end': 'url(#ah)' })
      .forEach(([k, v]) => line.setAttribute(k, v));
    svg.appendChild(line);
  }
}
"""


def make_root(tmp: Path) -> Path:
    for name in ("inventory", "base", "templates", "server"):
        shutil.copytree(REPO / name, tmp / name)
    return tmp


def simulate_setup(root: Path) -> None:
    """ІМІТАЦІЯ кнопки «Створити на сервері»: інвентар і реєстрація справжні, а SSH,
    розгортання та WireGuard — заглушки (сервера тут немає)."""
    from fleet.render import Renderer

    def fake_deploy(inv, store, site, log=print):
        Renderer(inv, store).render_site(site)
        marker = root / "build" / "sites" / site.name / "deployed.json"
        marker.write_text(json.dumps({"host": site.host, "time": time.strftime("%Y-%m-%d %H:%M")}), "utf-8")

    bootstrap.ensure_ssh = lambda *a, **k: None
    bootstrap.deploy_site = fake_deploy
    bootstrap.install_pilot_tunnel = lambda *a, **k: None
    bootstrap.run_setup(root, SERVER, PILOT, SERIAL, log=lambda *_: None)


def annotate(page: Page, items: list[tuple]) -> None:
    """items: (елемент, підпис, бік[, {"num", "dy", "shift", "outside"}]).

    outside — контейнер, за межами якого ставиться підпис (щоб не закривав сусідні поля).
    Для елементів панелі НРК і боку left це панель автоматично.
    """
    aside = page.locator("#detail").bounding_box()
    data = []
    for num, (locator, text, side, *rest) in enumerate(items, 1):
        opts = rest[0] if rest else {}
        box = locator.bounding_box()
        if box is None:
            raise SystemExit(f"element not found for: {text}")
        outer = opts["outside"].bounding_box() if "outside" in opts else None
        if outer is None and side == "left" and locator.evaluate("(e) => !!e.closest('#detail')"):
            outer = aside
        data.append({"num": opts.get("num", num), "box": box, "text": text, "side": side,
                     "dy": opts.get("dy", 0), "shift": opts.get("shift", 0), "outer": outer})
    page.evaluate(OVERLAY_JS, data)


def shot(page: Page, name: str, clip: dict | None = None) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUT / name), clip=clip)
    page.evaluate("document.querySelectorAll('.anno').forEach((e) => e.remove())")
    print("  ", name)


def top_region(page: Page, below: str, extra: int = 40) -> dict:
    box = page.locator(below).bounding_box()
    return {"x": 0, "y": 0, "width": VIEWPORT["width"], "height": min(VIEWPORT["height"], box["y"] + box["height"] + extra)}


def around_aside(page: Page, step: int, extra_left: int = 360, extra_rows: int = 0) -> dict:
    """Кадр навколо кроку панелі НРК з місцем ліворуч для підписів."""
    aside = page.locator("#detail").bounding_box()
    steps = page.locator("#detail .step")
    first = steps.nth(step - 1).bounding_box()
    last = steps.nth(step - 1 + extra_rows).bounding_box()
    top = max(0, first["y"] - 110)
    bottom = min(VIEWPORT["height"], last["y"] + last["height"] + 110)
    left = max(0, aside["x"] - extra_left)
    return {"x": left, "y": top, "width": aside["x"] + aside["width"] + 12 - left, "height": bottom - top}


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_root(Path(tmp))
        bootstrap.WG_EXE = Path(sys.executable)  # ІМІТАЦІЯ: «WireGuard встановлено»
        server = UiServer(("127.0.0.1", 0), root)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}/"

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=1.5, color_scheme="light")
            ctx.add_init_script("localStorage.setItem('fleet-lang', 'uk')")
            page = ctx.new_page()
            main_area = page.locator("main")

            def load(hash_: str = "") -> None:
                # Новий query щоразу: перехід лише за #якорем не перезавантажує сторінку
                page.goto(f"{base}?t={time.time()}{hash_}")
                page.wait_for_selector("#setup .wizard")
                page.wait_for_timeout(300)

            # 1. Майстер на новому ноутбуці: сервер, пілот і НРК
            load()
            page.get_by_label("Адреса сервера").fill(SERVER)
            page.get_by_label("Ім'я пілота").fill(PILOT)
            page.get_by_label("Заводський номер НРК").fill(SERIAL)
            step1 = page.locator("#setup .step").nth(0)
            annotate(page, [
                (step1.locator(".form"), "Адреса сервера, ваше ім'я пілота, номер НРК. Пароль SSH — лише першого разу",
                 "right", {"outside": main_area}),
                (step1.get_by_role("button", name="Створити на сервері"), "Одна кнопка: сервер, пілот, НРК і WireGuard",
                 "right", {"outside": main_area}),
            ])
            shot(page, "01-setup.png", top_region(page, "#setup-section", 60))

            # 2. Після «Створити на сервері» (ІМІТАЦІЯ сервера)
            simulate_setup(root)
            load("#robot-1")
            steps = page.locator("#setup .step")
            annotate(page, [
                (steps.nth(0).locator(".summary"), "Сервер розгорнуто, пілот і НРК створено", "bottom", {"shift": 260}),
                (steps.nth(1).get_by_role("button", name="Додати у WireGuard"), "Тунель пілота (якщо не додався сам)", "right"),
            ])
            shot(page, "02-setup-done.png", top_region(page, "#setup-section", 60))

            # 3. Тунель працює: майстер згортається, далі — панель НРК
            bootstrap.tunnel_status = lambda name: "running"  # ІМІТАЦІЯ: тунель піднято
            load("#robot-1")
            annotate(page, [
                (page.locator("#setup .wizard"), "Налаштування завершено", "bottom"),
                (page.locator("#detail .step").nth(1).locator(".step-title"), "Далі — роутер", "left"),
            ])
            shot(page, "03-next.png")

            # 4. MikroTik
            step = page.locator("#detail .step").nth(1)
            annotate(page, [
                (step.locator("input"), "Пароль з наліпки роутера (якщо є)", "left", {"dy": -40}),
                (step.get_by_role("button", name="Залити конфіг"), "Скинути роутер із конфігом НРК", "left", {"dy": 40}),
            ])
            shot(page, "04-mikrotik.png", around_aside(page, 2))

            # 5. Камери
            step = page.locator("#detail .step").nth(2)
            step.get_by_role("button", name="Показати пароль").click()
            page.wait_for_timeout(300)
            annotate(page, [
                (step.locator(".reveal"), "Пароль для активації камер (SADP)", "left", {"dy": -30}),
                (step.get_by_role("button", name="Перенести на адреси НРК"), "Камери в ether3 / ether4, потім сюди", "left", {"dy": 20}),
            ])
            shot(page, "05-cameras.png", around_aside(page, 3))

            # 6. Польотний контролер
            step = page.locator("#detail .step").nth(3)
            step.locator("input").fill("COM5")
            annotate(page, [
                (step.locator("input"), "COM-порт ФК (USB)", "left"),
                (step.locator(".line").last, "Порівняти або залити й перезавантажити", "left", {"dy": 20}),
            ])
            shot(page, "06-fc.png", around_aside(page, 4))

            # 7. Приймальний тест
            step = page.locator("#detail .step").nth(5)
            annotate(page, [
                (step.get_by_role("button", name="Запустити тест"), "Коли тунель пілота ввімкнено", "left"),
            ])
            shot(page, "07-test.png", around_aside(page, 6))

            # 8. Файли: конфіг для Netinstall
            files = page.locator("#detail .step").nth(7)
            annotate(page, [
                (files.locator(".files"), "robot.rsc — конфіг для Netinstall (Configure script)", "left"),
            ])
            shot(page, "08-files.png", around_aside(page, 7, extra_rows=1))

            # 9. Перемикач мови
            annotate(page, [(page.locator(".lang").first, "Мова інтерфейсу: UK / EN", "bottom")])
            top = page.locator(".top").bounding_box()
            shot(page, "09-lang.png", {"x": VIEWPORT["width"] - 760, "y": 0, "width": 760, "height": top["height"] + 120})

            page.locator(".lang button[data-lang=en]").click()
            page.wait_for_timeout(500)
            shot(page, "10-english.png")
            browser.close()
        server.shutdown()


if __name__ == "__main__":
    main()
