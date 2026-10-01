"""Browser regressions for the submission UI, using isolated, explicitly mocked APIs."""
import base64
import importlib.util
import json
import threading
import unittest
from pathlib import Path

from flask import Flask, render_template
from werkzeug.serving import make_server


HAS_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None
ROOT = Path(__file__).resolve().parents[1]
TRADE = {
    "stock": "INFY",
    "quantity": 2,
    "buy_price": 1500,
    "sell_price": "",
    "buy_date": "2026-09-12",
    "sell_date": "",
}
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9XcAAAAASUVORK5CYII="
)


@unittest.skipUnless(HAS_PLAYWRIGHT, "Install requirements-dev.txt and Playwright Chromium for browser regressions.")
class SubmissionBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import Error, sync_playwright

        app = Flask("frontend-fixture", template_folder=str(ROOT / "templates"), static_folder=str(ROOT / "static"))
        app.secret_key = "isolated-frontend-fixture"
        app.jinja_env.filters.update(money=str, num=str)
        app.jinja_env.globals["csrf_token"] = lambda: "fixture-csrf-token"

        @app.get("/submit/fixture")
        def portal():
            return render_template(
                "upload.html", admin=False, logged_in=True, need_code=False,
                portal_token="fixture", today="2026-09-30",
            )

        @app.post("/api/parse")
        def api_parse():
            return {"error": "This endpoint must be mocked in UI tests."}, 501

        @app.post("/api/submit")
        def api_submit():
            return {"error": "This endpoint must be mocked in UI tests."}, 501

        cls.server = make_server("127.0.0.1", 0, app)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch()
        except Error as error:
            cls.playwright.stop()
            cls.server.shutdown()
            cls.server_thread.join()
            if "Executable doesn't exist" in str(error):
                raise unittest.SkipTest("Run python -m playwright install chromium.") from error
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server_thread.join()
        cls.server.server_close()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 390, "height": 850})
        self.page = self.context.new_page()
        self.js_errors = []
        self.page.on("pageerror", lambda error: self.js_errors.append(str(error)))
        self.page.goto(self.base + "/submit/fixture", wait_until="networkidle")

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.js_errors, [])

    def fill_trade(self, **overrides):
        self.page.locator("#name").fill("Test Client")
        for field, value in (TRADE | overrides).items():
            if value != "":
                self.page.locator(f'[data-column="{field}"]').fill(str(value))

    def test_portal_is_standalone_even_with_admin_context_and_has_no_mobile_overflow(self):
        self.assertEqual(self.page.locator("#sidebar").count(), 0)
        self.assertFalse(self.page.evaluate("document.documentElement.scrollWidth > innerWidth"))
        self.assertEqual(self.page.locator(".sheet-wrap").count(), 1)
        self.assertGreater(self.page.locator(".sheet").bounding_box()["width"], 390)

    def test_missing_date_and_review_consent_prevent_submission(self):
        requests = []
        self.page.route("**/api/submit", lambda route: requests.append(route.request.post_data_json))
        self.fill_trade(buy_date="")
        self.page.locator("#submit-trades").click()
        self.assertEqual(requests, [])
        self.assertEqual(self.page.locator('[data-column="buy_date"]').get_attribute("aria-invalid"), "true")
        self.page.locator('[data-column="buy_date"]').fill("2026-09-12")
        self.page.locator("#submit-trades").click()
        self.assertEqual(requests, [])
        self.assertIn("confirm", self.page.locator("#submission-result").inner_text())

    def test_unknown_save_outcome_locks_edits_and_retries_identical_payload(self):
        requests = []

        def submit(route):
            payload = route.request.post_data_json
            requests.append(payload)
            result = {"error": "Temporary interruption."} if len(requests) == 1 else {
                "ok": True, "saved": 1, "submission_id": payload["submission_id"],
            }
            route.fulfill(status=500 if len(requests) == 1 else 200,
                          content_type="application/json", body=json.dumps(result))

        self.page.route("**/api/submit", submit)
        self.fill_trade()
        self.page.locator("#reviewed").check()
        self.page.locator("#submit-trades").click()
        self.page.locator("#retry-submit").wait_for(state="visible")
        self.assertTrue(self.page.locator("#name").is_disabled())
        self.page.locator("#retry-submit").click()
        self.page.locator("#submission-success").wait_for(state="visible")
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0], requests[1])
        self.page.locator("#send-another").click()
        self.assertTrue(self.page.locator("#name").is_enabled())
        self.assertEqual(self.page.locator('[data-column="stock"]').input_value(), "")

    def test_known_validation_error_preserves_rows_and_does_not_render_html(self):
        self.page.route("**/api/submit", lambda route: route.fulfill(
            status=400, content_type="application/json",
            body=json.dumps({"error": 'Invalid <img src=x onerror="alert(1)"> stock.'}),
        ))
        self.fill_trade()
        self.page.locator("#reviewed").check()
        self.page.locator("#submit-trades").click()
        self.page.wait_for_function("document.querySelector('#submission-result').textContent.includes('Invalid')")
        self.assertTrue(self.page.locator("#name").is_enabled())
        self.assertEqual(self.page.locator('[data-column="stock"]').input_value(), "INFY")
        self.assertEqual(self.page.locator("#submission-result img").count(), 0)
        self.assertFalse(self.page.locator("#retry-submit").is_visible())

    def test_scanner_start_failure_recovers_without_losing_the_image(self):
        self.page.evaluate("""() => {
          window.Tesseract = {createWorker: async () => {throw new Error('Scanner unavailable');}};
        }""")
        self.page.locator("#files").set_input_files({"name": "fixture.png", "mimeType": "image/png", "buffer": PNG})
        self.page.wait_for_function("document.querySelector('#scan-status').textContent.includes('Scanner unavailable')")
        self.assertTrue(self.page.locator("#name").is_enabled())
        self.assertEqual(self.page.locator(".thumb").count(), 1)
        self.assertTrue(self.page.locator("#scan-progress").is_hidden())

    def test_scanning_another_batch_does_not_overwrite_manual_corrections(self):
        requests = []
        self.page.evaluate("""() => {
          window.Tesseract = {createWorker: async () => ({
            setParameters: async () => {}, terminate: async () => {},
            recognize: async () => ({data: {text: 'INFY Buy Qty 2 Avg 1500 12 Sep 2026'}})
          })};
        }""")

        def parse(route):
            requests.append(route.request.post_data_json)
            route.fulfill(content_type="application/json", body=json.dumps({"platform": "generic", "trades": [TRADE]}))

        self.page.route("**/api/parse", parse)
        self.page.locator("#files").set_input_files({"name": "first.png", "mimeType": "image/png", "buffer": PNG})
        self.page.wait_for_function("document.querySelector('[data-column=stock]').value === 'INFY'")
        self.page.locator('[data-column="buy_price"]').fill("1600")
        self.page.locator("#files").set_input_files({"name": "second.png", "mimeType": "image/png", "buffer": PNG})
        self.page.wait_for_function("document.querySelector('#scan-status').textContent.includes('Your edits are untouched')")
        self.assertEqual(len(requests), 1)
        self.assertEqual(self.page.locator('[data-column="buy_price"]').input_value(), "1600")
        self.assertEqual(self.page.locator(".thumb").count(), 2)
        self.assertFalse(self.page.locator("#reviewed").is_checked())

    def test_images_upload_before_atomic_submission_and_keep_signed_receipts(self):
        events = []
        self.page.evaluate("""() => {
          window.Tesseract = {createWorker: async () => ({
            setParameters: async () => {}, terminate: async () => {},
            recognize: async () => ({data: {text: 'INFY Buy Qty 2 Avg 1500 12 Sep 2026'}})
          })};
        }""")
        self.page.route("**/api/parse", lambda route: route.fulfill(
            content_type="application/json", body=json.dumps({"platform": "generic", "trades": [TRADE]}),
        ))

        def sign(route):
            events.append("sign")
            self.assertEqual(route.request.headers["x-csrf-token"], "fixture-csrf-token")
            route.fulfill(content_type="application/json", body=json.dumps({
                "upload_url": self.base + "/upload-bytes/fixture", "method": "PUT",
                "path": "evidence/fixture.png", "receipt": "signed-receipt",
            }))

        def put_image(route):
            events.append("image")
            route.fulfill(status=204)

        def submit(route):
            events.append("submit")
            data = route.request.post_data_json
            self.assertEqual(data["images"], [{"path": "evidence/fixture.png", "receipt": "signed-receipt"}])
            self.assertEqual(data["rows"][0]["stock"], "INFY")
            route.fulfill(content_type="application/json", body='{"ok":true,"saved":1}')

        self.page.route("**/api/uploads/sign", sign)
        self.page.route("**/upload-bytes/fixture", put_image)
        self.page.route("**/api/submit", submit)
        self.page.locator("#name").fill("Test Client")
        self.page.locator("#files").set_input_files({"name": "fixture.png", "mimeType": "image/png", "buffer": PNG})
        self.page.wait_for_function("document.querySelector('[data-column=stock]').value === 'INFY'")
        self.assertEqual(events, [])
        self.page.locator("#reviewed").check()
        self.page.locator("#submit-trades").click()
        self.page.locator("#submission-success").wait_for(state="visible")
        self.assertEqual(events, ["sign", "image", "submit"])


if __name__ == "__main__":
    unittest.main()
