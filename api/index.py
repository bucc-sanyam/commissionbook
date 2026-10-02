"""Vercel's WSGI entrypoint; all state lives in PostgreSQL and private Storage."""

import sys
from flask import Flask, render_template_string

try:
    from app import app
except Exception as err:
    app = Flask(__name__)
    error_message = str(err)

    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def catch_all(path):
        return render_template_string("""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Configuration / Connection Error</title>
            <style>
                body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #f8fafc; padding: 2rem; }
                .card { max-width: 650px; margin: 40px auto; background: #1e293b; border-radius: 12px; padding: 28px; box-shadow: 0 10px 25px rgba(0,0,0,0.3); border: 1px solid #334155; }
                h1 { color: #f43f5e; font-size: 1.5rem; margin-top: 0; }
                p { color: #94a3b8; line-height: 1.6; }
                pre { background: #0f172a; padding: 16px; border-radius: 8px; color: #fb7185; overflow-x: auto; font-size: 0.9rem; border: 1px solid #334155; white-space: pre-wrap; }
            </style>
        </head>
        <body>
            <div class="card">
                <h1>⚠️ Application Setup Error</h1>
                <p>The application encountered an issue while initializing on Vercel:</p>
                <pre>{{ error }}</pre>
                <p>Please check your Vercel Environment Variables to resolve this issue.</p>
            </div>
        </body>
        </html>
        """, error=error_message), 500
