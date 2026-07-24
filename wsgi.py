from app import create_app

app = create_app()

if __name__ == "__main__":
    # 0.0.0.0 for LAN demos; Werkzeug debugger stays off (remote code
    # execution risk when exposed). Reloader alone is safe.
    app.run(host="0.0.0.0", port=5001, debug=False, use_reloader=True)
