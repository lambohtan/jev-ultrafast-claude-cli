"""Local-browser freshness/execution regressions. No model calls or external websites."""

from urllib.parse import quote

from jev_ultrafast.browser import Browser, StalePage

HTML = """<!doctype html><title>Guard checks</title>
<style>body{margin:30px}button{width:180px;height:50px}#outside{position:absolute;top:3000px}</style>
<p id="context">Cart total: $10</p>
<button id="target" onclick="window.clicks=(window.clicks||0)+1">Continue</button>
<label>City<input id="field" value="Zurich"></label>
<label><input id="toggle" type="checkbox">Refundable</label>
<select aria-label="Category"><option>All</option><option>Design</option></select>
<p id="outside">Unrelated offscreen text</p>"""


def main():
    browser = Browser("data:text/html," + quote(HTML))
    passed = []
    try:
        page = browser.observe(screenshot=False)
        action = next(a for a in page["actions"] if a["label"] == "Continue")
        browser.evaluate("document.querySelector('#target').style.transform='translateX(200px)'")
        assert browser.fresh(page), "Movement should use fresh geometry, not another model call"
        browser.act(action, page)
        assert browser.evaluate("window.clicks") == 1
        passed.append("moving target clicked at its current location")

        browser.evaluate("document.querySelector('#outside').textContent='Updated outside the viewport'")
        assert browser.fresh(page)
        passed.append("unrelated offscreen text does not invalidate")

        mutations = {
            "visible context": "document.querySelector('#context').textContent='Cart total: $100'",
            "accessible label": "document.querySelector('#target').setAttribute('aria-label','Delete account')",
            "field property": "document.querySelector('#field').value='London'",
            "checkbox property": "document.querySelector('#toggle').checked=true",
            "disabled target": "document.querySelector('#target').disabled=true",
            "read-only field": "document.querySelector('#field').readOnly=true",
            "hidden target": "document.querySelector('#target').style.display='none'",
            "replaced node": "document.querySelector('#target').outerHTML=document.querySelector('#target').outerHTML",
            "dropdown option": "document.querySelector('select').options[1].text='Coastal'",
        }
        for label, expression in mutations.items():
            browser.evaluate("document.querySelector('#target').style.display='block'; "
                             "document.querySelector('#target').disabled=false")
            page = browser.observe(screenshot=False)
            browser.evaluate(expression)
            assert not browser.fresh(page), label
            passed.append(label + " invalidates")

        browser.evaluate("document.querySelector('#target').disabled=false; "
                         "document.querySelector('#target').style.display='block'")
        page = browser.observe(screenshot=False)
        action = next(a for a in page["actions"] if a["label"] == "Delete account")
        # A textless overlay does not alter the model's semantic state, but must block a click.
        browser.evaluate("const cover=document.createElement('div'); "
                         "cover.style.cssText='position:fixed;inset:0;z-index:9999;background:white'; "
                         "document.body.append(cover)")
        assert browser.fresh(page)
        try:
            browser.act(action, page)
        except (RuntimeError, StalePage):
            pass
        else:
            raise AssertionError("Covered target was clicked")
        assert browser.evaluate("window.clicks") == 1
        passed.append("overlay blocked before input")

        browser.evaluate("document.body.innerHTML=" + repr("""
          <form><p id="price">Total $10</p>
          <button type="button" id="buy">Buy</button>
          <label>Search <input id="query" role="combobox" aria-controls="suggestions"></label>
          <div role="listbox" id="suggestions"></div>
          <label><input id="check" type="checkbox">Enabled</label>
          <label><input id="radio" type="radio">Choice</label>
          <input id="readonly" aria-label="Read only" readonly>
          <input id="secret" type="password" value="never expose this">
          <button id="off" disabled>Disabled</button>
          <select id="category" aria-label="Category">
            <option>All</option><option>Design</option><option disabled>Unavailable</option>
          </select>
          <input id="budget" type="range" min="0" max="100" step="5" value="0" aria-label="Max price"
                 aria-valuetext="$0" style="opacity:0;pointer-events:none">
          </form><aside id="unrelated">News</aside>
        """))
        page = browser.observe(screenshot=False)
        buy = next(a for a in page["actions"] if a["label"] == "Buy")
        browser.evaluate("document.querySelector('#unrelated').textContent='New unrelated news'")
        assert browser.fresh(page, buy)
        assert not browser.fresh(page)
        passed.append("click guard accepts unrelated visible updates; terminal guard rejects them")
        for label, expression in {
            "nearby price": "document.querySelector('#price').textContent='Total $100'",
            "form value": "document.querySelector('#query').value='changed'",
            "form toggle": "document.querySelector('#check').checked=true",
            "target replacement": "document.querySelector('#buy').outerHTML=document.querySelector('#buy').outerHTML",
        }.items():
            page = browser.observe(screenshot=False)
            buy = next(a for a in page["actions"] if a["label"] == "Buy")
            browser.evaluate(expression)
            assert not browser.fresh(page, buy), label
            passed.append(label + " invalidates action-specific guard")

        page = browser.observe(screenshot=False)
        actions = page["actions"]
        for role in ("checkbox", "radio"):
            assert {a["kind"] for a in actions if a.get("role") == role} == {"click"}
        assert {a["kind"] for a in actions if a["label"] == "Read only"} == {"click"}
        assert not any(a["label"] == "Disabled" or a.get("value") == "never expose this" for a in actions)
        assert [a["value"] for a in actions if a["kind"] == "select"] == ["Design"]
        passed.append("native controls expose only supported operations and safe values")

        select = next(a for a in actions if a["kind"] == "select")
        browser.act(select, page)
        assert browser.evaluate("document.querySelector('#category').value") == "Design"
        passed.append("native dropdown selects an observed option")

        # A slider is routinely transparent and pointer-events:none under a custom thumb.
        page = browser.observe(screenshot=False)
        stops = [a for a in page["actions"] if a["kind"] == "range"]
        assert stops, "A transparent range input must still be observed"
        assert {a["role"] for a in stops} == {"slider"}
        assert all(a["current_value"] == "$0" for a in stops)
        values = [float(a["value"]) for a in stops]
        assert all(0 <= v <= 100 and v % 5 == 0 for v in values), values
        assert 0 not in values, "The position it already holds is not offered"
        assert len(values) == len(set(values)), values
        # It sits at its minimum, so every offer must move it up, and one must reach the far end.
        assert all(v > 0 for v in values) and 100 in values, values
        assert any("higher by 5% of its track" in a["label"] for a in stops), [a["label"] for a in stops]
        passed.append("transparent slider offers distinct relative moves, not its own position")

        browser.evaluate("window.events=[]; for (const type of ['input','change']) "
                         "document.querySelector('#budget').addEventListener(type,e=>window.events.push(e.type))")
        stop = next(a for a in stops if a["value"] == "50")
        browser.act(stop, page)
        assert browser.evaluate("document.querySelector('#budget').value") == "50"
        assert browser.evaluate("window.events") == ["input", "change"]
        passed.append("slider moves to an observed position and notifies the page")

        page = browser.observe(screenshot=False)
        stop = next(a for a in page["actions"] if a["kind"] == "range")
        try:
            browser.act({**stop, "value": "1000"}, page)
        except (RuntimeError, StalePage):
            pass
        else:
            raise AssertionError("A value outside the track was accepted")
        assert browser.evaluate("document.querySelector('#budget').value") == "50"
        passed.append("a value outside the track is rejected without moving the slider")

        page = browser.observe(screenshot=False)
        stop = next(a for a in page["actions"] if a["kind"] == "range")
        browser.evaluate("document.querySelector('#budget').value='20'")
        assert not browser.fresh(page, stop)
        passed.append("slider movement invalidates action-specific guard")

        browser.evaluate("document.querySelector('#query').addEventListener('input',()=>setTimeout(()=>{"
                         "document.querySelector('#suggestions').innerHTML='<div role=option>Generated</div>'"
                         "},60))")
        page = browser.observe(screenshot=False)
        field = next(a for a in page["actions"] if a["kind"] == "fill")
        browser.act(field, page, text="Generated")
        page = browser.observe(screenshot=False)
        value = browser.evaluate("document.querySelector('#query').value")
        assert value == "Generated", repr(value)
        assert any(a.get("role") == "option" for a in page["actions"])
        passed.append("real text input waits for asynchronous combobox suggestions")
        # A page routinely marks its whole app root aria-hidden while a dialog is open, and renders
        # the dialog inside that same root. A month strip clips its own overflow.
        browser.evaluate("document.body.innerHTML=" + repr("""
          <div id="app" aria-hidden="true">
            <button id="background">Background</button>
            <div role="dialog">
              <button id="confirm">Confirm</button>
              <span aria-hidden="true"><button>Decoration</button></span>
              <div style="width:100px;overflow:hidden;white-space:nowrap">
                <button style="width:80px">Shown day</button><button style="width:80px">Clipped day</button>
              </div>
            </div>
          </div>
        """))
        page = browser.observe(screenshot=False)
        labels = {a["label"] for a in page["actions"]}
        assert "Confirm" in labels, "A dialog inside an aria-hidden root is still on screen"
        assert "Background" not in labels, "The root's aria-hidden still hides what is outside the dialog"
        assert "Decoration" not in labels, "aria-hidden inside the dialog still hides"
        assert "Confirm" in page["text"], "Text in that dialog is readable too"
        passed.append("a dialog inside an aria-hidden root is observed; the root behind it is not")

        assert "Shown day" in labels and "Clipped day" not in labels, labels
        passed.append("a control scrolled out of an overflow container is not offered")

        browser.call("Page.navigate", url="about:blank")
        assert not browser.fresh(page, field)
        passed.append("navigation invalidates the old document")
    finally:
        browser.close()
    print("\n".join(passed))
    print(f"PASS: {len(passed)} browser guard checks; no model calls")


if __name__ == "__main__":
    main()
