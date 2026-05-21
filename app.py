from flask import Flask, request, jsonify, Response, send_from_directory
from flask_cors import CORS
import yt_dlp
import json
import os
import threading
import time
import re

app = Flask(__name__, static_folder='.')
CORS(app)

# On Railway, files go to /tmp (ephemeral but works for serving)
DOWNLOAD_DIR = os.path.join('/tmp', 'downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

progress_store = {}
active_threads = {}

def sanitize_id(session_id):
    return re.sub(r'[^a-zA-Z0-9_-]', '', session_id)[:64]

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/info', methods=['POST'])
def get_info():
    data = request.json or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    try:
        opts = {
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': 15,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return jsonify({
                'title':      info.get('title', 'Unknown'),
                'thumbnail':  info.get('thumbnail', ''),
                'duration':   info.get('duration', 0),
                'uploader':   info.get('uploader', 'Unknown'),
                'view_count': info.get('view_count', 0),
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/download', methods=['POST'])
def start_download():
    data = request.json or {}
    url        = data.get('url', '').strip()
    session_id = sanitize_id(data.get('session_id', str(time.time())))

    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    if session_id in active_threads and active_threads[session_id].is_alive():
        return jsonify({'error': 'Download already in progress'}), 409

    progress_store[session_id] = {
        'status': 'starting', 'percent': 0,
        'speed': '', 'eta': '', 'filename': ''
    }

    def progress_hook(d):
        if d['status'] == 'downloading':
            raw = d.get('_percent_str', '0%').strip()
            raw = re.sub(r'\x1b\[[0-9;]*m', '', raw).replace('%', '')
            try:
                pct = float(raw)
            except ValueError:
                pct = 0.0
            progress_store[session_id] = {
                'status':   'downloading',
                'percent':  round(min(pct, 99.9), 1),
                'speed':    d.get('_speed_str', '').strip(),
                'eta':      d.get('_eta_str', '').strip(),
                'filename': d.get('filename', ''),
            }
        elif d['status'] == 'finished':
            progress_store[session_id] = {
                'status':   'processing',
                'percent':  100,
                'speed':    '',
                'eta':      '',
                'filename': d.get('filename', ''),
            }

    def run_download():
        try:
            ydl_opts = {
                'format':              'best',
                'outtmpl':             os.path.join(DOWNLOAD_DIR, '%(title)s.%(ext)s'),
                'progress_hooks':      [progress_hook],
                'quiet':               True,
                'no_warnings':         True,
                'socket_timeout':      30,
                'merge_output_format': 'mp4',
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            progress_store[session_id]['status'] = 'done'
        except Exception as e:
            progress_store[session_id] = {
                'status': 'error',
                'error':  str(e),
                'percent': 0,
            }
        finally:
            active_threads.pop(session_id, None)

    thread = threading.Thread(target=run_download, name=f'dl-{session_id}', daemon=False)
    active_threads[session_id] = thread
    thread.start()

    return jsonify({'session_id': session_id, 'status': 'started'})

@app.route('/progress/<session_id>')
def progress_stream(session_id):
    session_id = sanitize_id(session_id)

    def generate():
        last_data  = None
        start_time = time.time()
        max_wait   = 600

        while time.time() - start_time < max_wait:
            current = progress_store.get(session_id)
            if current is None:
                yield 'data: {"status":"error","error":"Session not found"}\n\n'
                break
            if current != last_data:
                last_data = dict(current)
                yield f"data: {json.dumps(current)}\n\n"
            if current.get('status') in ('done', 'error'):
                break
            time.sleep(0.4)

        progress_store.pop(session_id, None)

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control':     'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection':        'keep-alive',
        }
    )

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"\n🎬 YouTube Downloader  →  http://localhost:{port}\n")
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)