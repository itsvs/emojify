import json
import re
import traceback

import requests
from flask import jsonify, request, redirect

from auth import query
from config_client import (
    CONFIG,
    REJECTED,
    UNABLE,
    get_user_token,
    store_user_token,
    store_bot_token,
    get_team_data,
)
from db import connect_db
from emoji_integration import EmojiIntegration
from env import CLIENT_ID, CLIENT_SECRET
from ethical_integration import Ethicalntegration
from golink_integration import GoLinkIntegration
from integration import combine_integrations
from piazza_integration import PiazzaIntegration
from promotions import make_promo_block
from security import slack_signed

WORKSPACE_CACHE = {}


def get_course(workspace):
    if workspace not in WORKSPACE_CACHE:
        for course in CONFIG:
            try:
                WORKSPACE_CACHE[query("/slack/workspace_name", course=course)] = course
            except KeyError:
                continue
    return WORKSPACE_CACHE[workspace]


def create_slack_client(app):
    @app.route("/oauth")
    def oauth():
        if not request.args["code"]:
            return jsonify({"Error": "sadcat"}), 500
        resp = requests.post(
            "https://slack.com/api/oauth.v2.access",
            {
                "code": request.args["code"],
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
        )
        if resp.status_code == 200:
            data = resp.json()
            bot_token = data["access_token"]
            workspace_data = requests.post(
                "https://slack.com/api/auth.test",
                headers={"Authorization": "Bearer {}".format(bot_token)},
            ).json()
            workspace_url = workspace_data["url"]
            workspace = re.match(
                r"https://([a-zA-Z\-0-9]+)\.slack\.com", workspace_url
            ).group(1)
            store_bot_token(get_course(workspace), data["team"]["id"], bot_token)
            store_user_token(
                data["authed_user"]["id"], data["authed_user"]["access_token"]
            )
            with connect_db() as db:
                db("DELETE FROM silenced_users WHERE user = (%s)", [data["authed_user"]["id"]])
            return redirect(workspace_url)
        return jsonify({"Error": "sadcat"}), 500

    @app.route("/interactive_handler", methods=["POST"])
    @slack_signed
    def handler():
        payload = json.loads(request.form["payload"])
        if "actions" not in payload or "value" not in payload["actions"][0]:
            return ""
        action = payload["actions"][0]["value"]
        user_id = payload["user"]["id"]
        if action == "activate":
            requests.post(
                payload["response_url"],
                json={
                    "text": ":robot_face: Activated! While we can't update your previous message (:crying_cat_face:), all your future messages will be made awesome!",
                    "replace_original": "true",
                },
            )
        elif action == "maybe_later":
            requests.post(
                payload["response_url"],
                json={
                    "text": "Alright, I'll ask you again later. Or visit slack.apps.cs61a.org to activate this bot manually!!",
                    "replace_original": "true",
                },
            )
        elif action == "never_ask_again":
            with connect_db() as db:
                db("INSERT INTO silenced_users VALUES (%s)", (user_id,))
            requests.post(
                payload["response_url"],
                json={
                    "text": "Understood. If you ever change your mind, visit slack.apps.cs61a.org to activate this bot!",
                    "replace_original": "true",
                },
            )

        return ""

    @app.route("/message_send", methods=["POST"])
    @slack_signed
    def message_send():
        d = request.json
        try:
            if "challenge" in d:
                return
            team_id = d["team_id"]
            course, bot_token = get_team_data(team_id)
            event = d["event"]

            if event["type"] == "channel_created":
                print(
                    requests.post(
                        "https://slack.com/api/conversations.join",
                        json={"channel": event["channel"]["id"]},
                        headers={"Authorization": "Bearer {}".format(bot_token)},
                    ).json()
                )
                return

            token = get_user_token(event["user"])
            if token is REJECTED:
                return
            if "edited" in event:
                return
            if "subtype" in event:
                return

            features = CONFIG[course]["features"]

            integrations = []
            if features.get("piazza"):
                integrations.append(PiazzaIntegration)
            if features.get("emojify"):
                integrations.append(EmojiIntegration)
            if features.get("golinks"):
                integrations.append(GoLinkIntegration)
            if features.get("fun"):
                integrations.append(Ethicalntegration)

            combined_integration = combine_integrations(integrations)(
                event["text"], token if token is not UNABLE else None, team_id
            )

            if (
                combined_integration.message != event["text"]
                or combined_integration.attachments
            ):
                if token is not UNABLE:
                    resp = requests.post(
                        "https://slack.com/api/chat.update",
                        json={
                            "channel": event["channel"],
                            "ts": event["ts"],
                            "as_user": True,
                            "text": combined_integration.message,
                            "attachments": combined_integration.attachments,
                        },
                        headers={"Authorization": "Bearer {}".format(token)},
                    ).json()
                    if not resp["ok"] and resp["error"] in {
                        "invalid_auth",
                        "token_revoked",
                        "account_inactive",
                        "missing_scope",
                    }:
                        # token available, but no permissions
                        token = UNABLE

                if token is UNABLE or "slack_force" in event["text"]:
                    requests.post(
                        "https://slack.com/api/chat.postEphemeral",
                        json={
                            "blocks": make_promo_block(combined_integration.message),
                            "attachments": [],
                            "channel": event["channel"],
                            "user": event["user"],
                            "username": "61A Slackbot"
                        },
                        headers={"Authorization": "Bearer {}".format(bot_token)},
                    ).json()

        except Exception as e:
            print("".join(traceback.TracebackException.from_exception(e).format()))
        finally:
            if "challenge" in d:
                return d["challenge"]
            return ""