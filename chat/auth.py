"""
chat/auth.py
Simple password-based authentication for the chat UI.
"""

import secrets
import hashlib
from functools import wraps
from flask import request, session, redirect, url_for, jsonify


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            if request.is_json:
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated
