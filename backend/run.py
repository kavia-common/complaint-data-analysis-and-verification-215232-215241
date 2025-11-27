from app import app

if __name__ == "__main__":
    # Bind to port 3001 for backend as per environment setup
    app.run(host="0.0.0.0", port=3001)
