"""Agency command center — separately specced; reads this DB. Stub route
reserves the blueprint and URL space."""
from flask import Blueprint, jsonify

bp = Blueprint("agency", __name__)


@bp.get("/")
def home():
    return jsonify(status="agency command center — separate brief, shares this DB")
