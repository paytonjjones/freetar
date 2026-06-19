import waitress
import os
import argparse
import sys
from flask import Flask, render_template, request, jsonify
from flask_caching import Cache
from flask_minify import Minify

from freetar.ug import Search, ug_tab, configure_ug, clear_disk_cache
from freetar.utils import get_version, FreetarError

CACHE_TIMEOUT = int(os.environ.get("FREETAR_CACHE_TIMEOUT", 0))
cache = Cache(config={'CACHE_TYPE': 'SimpleCache',
                      "CACHE_DEFAULT_TIMEOUT": CACHE_TIMEOUT,
                      "CACHE_THRESHOLD": 10000})

app = Flask(__name__)
cache.init_app(app)
Minify(app=app, html=True, js=True, cssless=True)


@app.context_processor
def export_variables():
    return {
        'version': get_version(),
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/search")
@cache.cached(query_string=True)
def search():
    search_term = request.args.get("search_term")
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        return render_template('error.html',
                               error="Invalid page requested. Not a number.")
    search_results = None
    if search_term:
        search_results = Search(search_term, page)
    return render_template("index.html",
                           search_term=search_term,
                           title=f"Freetar - Search: {search_term}",
                           search_results=search_results)


@app.route("/tab/<artist>/<song>")
@cache.cached()
def show_tab(artist: str, song: str):
    tab = ug_tab(f"{artist}/{song}")
    return render_template("tab.html",
                           tab=tab,
                           title=f"{tab.artist_name} - {tab.song_name}")


@app.route("/tab/<tabid>")
@cache.cached()
def show_tab2(tabid: int):
    tab = ug_tab(tabid)
    return render_template("tab.html",
                           tab=tab,
                           title=f"{tab.artist_name} - {tab.song_name}")


@app.route("/about")
def show_about():
    return render_template('about.html')


@app.route("/cache/clear", methods=["POST"])
def clear_cache_all():
    result = clear_disk_cache()
    return jsonify(result)


@app.route("/cache/clear_keep_favorites", methods=["POST"])
def clear_cache_keep_favorites():
    payload = request.get_json(silent=True) or {}
    result = clear_disk_cache(payload.get("favorites", []))
    return jsonify(result)


@app.errorhandler(403)
@app.errorhandler(500)
@app.errorhandler(FreetarError)
def internal_error(error):
    search_term = request.args.get("search_term")
    return render_template('error.html',
                           search_term=search_term,
                           error=error)


def _parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true",
                        help="Fetch Ultimate Guitar pages directly instead of through the public Freetar proxies.")
    parser.add_argument("--cache", action="store_true",
                        help="Persist raw upstream tab/search responses on disk.")
    args = sys.argv[1:]
    if args and args[0] == "--":
        args = args[1:]
    return parser.parse_args(args)


def main():
    args = _parse_args()
    host = os.getenv('FREETAR_HOST', '0.0.0.0')
    port = os.getenv('FREETAR_PORT', 22000)
    configure_ug(
        local=args.local or os.environ.get("FREETAR_LOCAL", "").lower() in ("1", "true", "yes", "on"),
        disk_cache=args.cache or os.environ.get("FREETAR_DISK_CACHE", "").lower() in ("1", "true", "yes", "on"),
        disk_cache_dir=os.environ.get("FREETAR_DISK_CACHE_DIR"),
    )
    if __name__ == '__main__':
        app.run(debug=True,
                host=host,
                port=port)
    else:
        threads = os.environ.get("THREADS", "4")
        print(f"Running freetar {get_version()} backend on {host}:{port} with {threads} threads")
        waitress.serve(app, listen=f"{host}:{port}", threads=threads)


if __name__ == '__main__':
    main()
