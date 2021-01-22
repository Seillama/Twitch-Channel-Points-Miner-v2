import logging
import threading
import time
import json
import random

from dateutil import parser

from TwitchChannelPointsMiner.classes.EventPrediction import EventPrediction
from TwitchChannelPointsMiner.classes.Raid import Raid
from TwitchChannelPointsMiner.classes.TwitchWebSocket import TwitchWebSocket

logger = logging.getLogger(__name__)


def get_streamer_index(streamers, channel_id):
    try:
        return next(
            i for i, x in enumerate(streamers) if str(x.channel_id) == str(channel_id)
        )
    except StopIteration:
        return -1


class WebSocketsPool:
    def __init__(
        self,
        twitch,
        twitch_browser,
        streamers,
        bet_settings,
        events_predictions,
        less_printing: bool = False,
    ):
        self.ws = None
        self.twitch = twitch
        self.twitch_browser = twitch_browser
        self.streamers = streamers
        self.events_predictions = events_predictions
        self.bet_settings = bet_settings

        self.less_printing = less_printing

    """
    API Limits
    - Clients can listen on up to 50 topics per connection. Trying to listen on more topics will result in an error message.
    - We recommend that a single client IP address establishes no more than 10 simultaneous connections.
    The two limits above are likely to be relaxed for approved third-party applications, as we start to better understand third-party requirements.
    """

    def submit(self, topic):
        if self.ws is None or len(self.ws.topics) >= 50:
            self.create_new_websocket()

        self.ws.topics.append(topic)

        if not self.ws.is_opened:
            self.ws.pending_topics.append(topic)
        else:
            self.ws.listen(topic, self.twitch.twitch_login.get_auth_token())

    def create_new_websocket(self):
        self.ws = TwitchWebSocket(
            "wss://pubsub-edge.twitch.tv/v1",
            on_message=WebSocketsPool.on_message,
            on_open=WebSocketsPool.on_open,
            on_close=WebSocketsPool.handle_websocket_reconnection,
        )
        self.ws.reset(self)

        self.thread_ws = threading.Thread(target=lambda: self.ws.run_forever())
        self.thread_ws.daemon = True
        self.thread_ws.start()

    def end(self):
        self.ws.keep_running = False
        self.ws.close()

    @staticmethod
    def on_open(ws):
        def run():
            ws.is_opened = True
            ws.ping()
            for topic in ws.pending_topics:
                ws.listen(topic, ws.twitch.twitch_login.get_auth_token())

            while not ws.is_closed:
                ws.ping()
                time.sleep(random.uniform(25, 30))

                if ws.elapsed_last_pong() > 5:
                    logger.info(
                        "The last pong was received more than 5 minutes ago. Reconnect the WebSocket"
                    )
                    WebSocketsPool.handle_websocket_reconnection(ws)

        thread_ws = threading.Thread(target=run)
        thread_ws.daemon = True
        thread_ws.start()

    @staticmethod
    def handle_websocket_reconnection(ws):
        ws.is_closed = True
        if ws.keep_running is True:
            logger.info("Reconnecting to Twitch PubSub server in 60 seconds")
            time.sleep(60)

            self = ws.parent_pool
            if self.ws == ws:
                self.ws = None
            for topic in ws.topics:
                self.submit(topic)

    @staticmethod
    def on_message(ws, message):
        logger.debug(f"Received: {message.strip()}")
        response = json.loads(message)

        if response["type"] == "MESSAGE":
            data = response["data"]
            topic, topic_user = data["topic"].split(".")

            message = json.loads(data["message"])
            message_type = message["type"]

            message_data = None
            if "data" in message:
                message_data = message["data"]

            # If we have more than one PubSub connection, messages may be duplicated
            if (
                time.time() - ws.last_message_time < 0.1
                and ws.last_message_type == message_type
            ):
                ws.last_message_time = time.time()
                return

            ws.last_message_time = time.time()
            ws.last_message_type = message_type

            try:
                if topic == "community-points-user-v1":
                    if message_type == "points-earned":
                        streamer_index = get_streamer_index(
                            ws.streamers, message_data["channel_id"]
                        )
                        if streamer_index != -1:
                            earned = message_data["point_gain"]["total_points"]
                            reason_code = message_data["point_gain"]["reason_code"]
                            balance = message_data["balance"]["balance"]
                            ws.streamers[streamer_index].channel_points = balance
                            logger.info(
                                f"+{earned} → {ws.streamers[streamer_index]} - Reason: {reason_code}.",
                                extra={"emoji": ":rocket:"},
                            )
                            ws.streamers[streamer_index].update_history(
                                reason_code, earned
                            )
                    elif message_type == "claim-available":
                        streamer_index = get_streamer_index(
                            ws.streamers, message_data["claim"]["channel_id"]
                        )
                        if streamer_index != -1:
                            ws.twitch.claim_bonus(
                                ws.streamers[streamer_index],
                                message_data["claim"]["id"],
                            )

                elif topic == "video-playback-by-id":
                    streamer_index = get_streamer_index(ws.streamers, topic_user)
                    if streamer_index != -1:
                        if message_type == "stream-down":
                            ws.streamers[streamer_index].set_offline()
                        elif message_type == "viewcount":
                            ws.twitch.check_streamer_online(
                                ws.streamers[streamer_index]
                            )
                        # There is stream-up message type, but it's sent earlier than the API updates

                elif topic == "raid":
                    streamer_index = get_streamer_index(ws.streamers, topic_user)
                    if streamer_index != -1:
                        if message_type == "raid_update_v2":
                            raid = Raid(
                                message["raid"]["id"], message["raid"]["target_login"]
                            )
                            ws.twitch.update_raid(ws.streamers[streamer_index], raid)

                elif topic == "predictions-channel-v1":

                    # message_data["event"]["channel_id"]
                    # if message_type == "event-created"
                    # if message_type == "event-updated"

                    streamer_index = get_streamer_index(ws.streamers, topic_user)
                    if streamer_index != -1:
                        event_dict = message_data["event"]
                        event_id = event_dict["id"]
                        event_status = event_dict["status"]

                        current_timestamp = parser.parse(message_data["timestamp"])

                        if event_id not in ws.events_predictions:
                            if event_status == "ACTIVE":
                                time.sleep(random.uniform(0.5, 1.0))
                                prediction_window_seconds = float(
                                    event_dict["prediction_window_seconds"]
                                )
                                prediction_window_seconds -= (
                                    25 if prediction_window_seconds <= 120 else 50
                                )
                                event = EventPrediction(
                                    ws.streamers[streamer_index],
                                    event_id,
                                    event_dict["title"],
                                    parser.parse(event_dict["created_at"]),
                                    prediction_window_seconds,
                                    event_status,
                                    event_dict["outcomes"],
                                    bet_settings=ws.bet_settings,
                                    less_printing=ws.less_printing,
                                )
                                if (
                                    ws.streamers[streamer_index].is_online
                                    and event.closing_bet_after(current_timestamp) > 0
                                ):
                                    ws.events_predictions[event_id] = event
                                    if ws.twitch_browser.currently_is_betting is False:
                                        (
                                            start_bet_status,
                                            execution_time,
                                        ) = ws.twitch_browser.start_bet(
                                            ws.events_predictions[event_id]
                                        )
                                        if start_bet_status is True:
                                            # place_bet_thread = threading.Timer(event.closing_bet_after(current_timestamp), ws.twitch.make_predictions, (ws.events_predictions[event_id],))
                                            execution_time = round(execution_time, 2)
                                            start_after = (
                                                event.closing_bet_after(
                                                    current_timestamp
                                                )
                                                - execution_time
                                            )
                                            start_after = max(1, start_after)
                                            place_bet_thread = threading.Timer(
                                                start_after,
                                                ws.twitch_browser.place_bet,
                                                (ws.events_predictions[event_id],),
                                            )
                                            place_bet_thread.daemon = True
                                            place_bet_thread.start()

                                            logger.info(
                                                f"Place the bet after: {start_after}s for: {ws.events_predictions[event_id]}",
                                                extra={"emoji": ":alarm_clock:"},
                                            )
                                    else:
                                        logger.info(
                                            f"Sorry, unable to start {event}. The browser it's currently betting another event"
                                        )

                        else:
                            ws.events_predictions[event_id].status = event_status
                            # Game over we can't update anymore the values... The bet was placed!
                            if (
                                ws.events_predictions[event_id].bet_placed is False
                                and ws.events_predictions[event_id].bet.decision is None
                            ):
                                ws.events_predictions[event_id].bet.update_outcomes(
                                    event_dict["outcomes"]
                                )

                elif topic == "predictions-user-v1":
                    time.sleep(random.uniform(1, 2))
                    if message_type == "prediction-result":
                        event_id = message_data["prediction"]["event_id"]
                        event_result = message_data["prediction"]["result"]
                        if event_id in ws.events_predictions:
                            logger.info(
                                f"{ws.events_predictions[event_id]} - Result: {event_result['type']}, Points won: {event_result['points_won'] if event_result['points_won'] else 0}",
                                extra={"emoji": ":bar_chart:"},
                            )
                            points_won = (
                                event_result["points_won"]
                                if event_result["points_won"]
                                else 0
                            )
                            ws.events_predictions[event_id].final_result = {
                                "type": event_result["type"],
                                "won": points_won,
                            }
            except Exception:
                logger.error(
                    f"Exception raised for topic: {topic} and message: {message}",
                    exc_info=True,
                )

        elif response["type"] == "RESPONSE" and len(response.get("error", "")) > 0:
            raise RuntimeError(f"Error while trying to listen for a topic: {response}")

        elif response["type"] == "RECONNECT":
            logger.info(f"Reconnection required and keep running is: {ws.keep_running}")
            WebSocketsPool.handle_websocket_reconnection(ws)

        elif response["type"] == "PONG":
            ws.last_pong = time.time()
