import os

from backend.web import create_app


app = create_app()


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", 5050)),
        debug=os.environ.get("DEBUG") == "1" and not app.config["PRODUCTION"],
    )
