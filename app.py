"""
College Wi-Fi Network & Bandwidth Monitoring System
Flask Application Entrypoint for Vercel and Local Development
"""
from run import app

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
