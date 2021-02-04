import json
import logging
import random
import threading
import time

from dateutil import parser

from TwitchChannelPointsMiner.classes.entities.EventPrediction import EventPrediction
from TwitchChannelPointsMiner.classes.entities.Message import Message
from TwitchChannelPointsMiner.classes.entities.Raid import Raid
from TwitchChannelPointsMiner.classes.Exceptions import TimeBasedDropNotFound
from TwitchChannelPointsMiner.classes.TwitchWebSocket import TwitchWebSocket
from TwitchChannelPointsMiner.constants.twitch import WEBSOCKET
from TwitchChannelPointsMiner.utils import (
    _millify,
    bet_condition,
    calculate_start_after,
    get_streamer_index,
)

logger = logging.getLogger(__name__)


class WebSocketsPool:
    def __init__(self, twitch, browser, streamers, events_predictions):
        self.ws: list = []
        self.twitch = twitch
        self.browser = browser
        self.streamers = streamers
        self.events_predictions = events_predictions

    """
    API Limits
    - Clients can listen on up to 50 topics per connection. Trying to listen on more topics will result in an error message.
    - We recommend that a single client IP address establishes no more than 10 simultaneous connections.
    The two limits above are likely to be relaxed for approved third-party applications, as we start to better understand third-party requirements.
    """

    def submit(self, topic):
        if self.ws == [] or len(self.ws[-1].topics) >= 50:
            self.append_new_websocket()

        self.ws[-1].topics.append(topic)

        if self.ws[-1].is_opened is False:
            self.ws[-1].pending_topics.append(topic)
        else:
            self.ws[-1].listen(topic, self.twitch.twitch_login.get_auth_token())

    def append_new_websocket(self):
        self.ws.append(
            TwitchWebSocket(
                index=len(self.ws),
                parent_pool=self,
                url=WEBSOCKET,
                on_message=WebSocketsPool.on_message,
                on_open=WebSocketsPool.on_open,
                on_error=WebSocketsPool.on_error,
                on_close=WebSocketsPool.on_close
                # on_close=WebSocketsPool.handle_reconnection, # Do nothing.
            )
        )

        self.thread_ws = threading.Thread(target=lambda: self.ws[-1].run_forever())
        self.thread_ws.daemon = True
        self.thread_ws.start()

    def end(self):
        for index in range(0, len(self.ws)):
            if self.ws[index] is not None:
                self.ws[index].forced_close = True
                self.ws[index].close()

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

                if ws.elapsed_last_pong() > 10 and ws.is_reconneting is False:
                    logger.info(
                        f"#{ws.index} - The last PONG was received more than 10 minutes ago. Reconnect the WebSocket"
                    )
                    ws.is_reconneting = True
                    WebSocketsPool.handle_reconnection(ws)

        thread_ws = threading.Thread(target=run)
        thread_ws.daemon = True
        thread_ws.start()

    @staticmethod
    def on_error(ws, error):
        logger.error(f"#{ws.index} - WebSocket error: {error}")

    @staticmethod
    def on_close(ws):
        logger.info(f"#{ws.index} - WebSocket closed")
        # On close please reconnect automatically
        WebSocketsPool.handle_reconnection(ws)

    @staticmethod
    def handle_reconnection(ws):
        # Close the current WebSocket.
        # anyway, we replace the ws with None
        ws.is_closed = True
        ws.keep_running = False
        # Reconnect only if ws.forced_close is False (replace the keep_running)
        if ws.forced_close is False:
            logger.info(
                f"#{ws.index} - Reconnecting to Twitch PubSub server in 30 seconds"
            )
            time.sleep(30)

            self = ws.parent_pool
            self.ws[ws.index] = None
            for topic in ws.topics:
                self.submit(topic)

    @staticmethod
    def on_message(ws, message):
        logger.debug(f"#{ws.index} - Received: {message.strip()}")
        response = json.loads(message)

        if response["type"] == "MESSAGE":
            # We should create a Message class ...
            message = Message(response["data"])

            # If we have more than one PubSub connection, messages may be duplicated
            # Check the concatenation between message_type.top.channel_id
            if (
                ws.last_message_type_channel is not None
                and ws.last_message_timestamp is not None
                and ws.last_message_timestamp == message.timestamp
                and ws.last_message_type_channel == message.identifier
            ):
                return

            ws.last_message_timestamp = message.timestamp
            ws.last_message_type_channel = message.identifier

            streamer_index = get_streamer_index(ws.streamers, message.channel_id)
            if streamer_index != -1:
                try:
                    if message.topic == "community-points-user-v1":
                        if message.type == "points-earned":
                            earned = message.data["point_gain"]["total_points"]
                            reason_code = message.data["point_gain"]["reason_code"]
                            balance = message.data["balance"]["balance"]
                            ws.streamers[streamer_index].channel_points = balance
                            logger.info(
                                f"+{earned} → {ws.streamers[streamer_index]} - Reason: {reason_code}.",
                                extra={"emoji": ":rocket:"},
                            )
                            ws.streamers[streamer_index].update_history(
                                reason_code, earned
                            )
                        elif message.type == "claim-available":
                            ws.twitch.claim_bonus(
                                ws.streamers[streamer_index],
                                message.data["claim"]["id"],
                            )

                    elif message.topic == "video-playback-by-id":
                        # There is stream-up message type, but it's sent earlier than the API updates
                        if message.type == "stream-up":
                            ws.streamers[streamer_index].stream_up = time.time()
                        elif message.type == "stream-down":
                            if ws.streamers[streamer_index].is_online is True:
                                ws.streamers[streamer_index].set_offline()
                        elif message.type == "viewcount":
                            if ws.streamers[streamer_index].stream_up_elapsed():
                                ws.twitch.check_streamer_online(
                                    ws.streamers[streamer_index]
                                )

                    elif message.topic == "raid":
                        if message.type == "raid_update_v2":
                            raid = Raid(
                                message.message["raid"]["id"],
                                message.message["raid"]["target_login"],
                            )
                            ws.twitch.update_raid(ws.streamers[streamer_index], raid)

                    elif message.topic == "predictions-channel-v1":

                        event_dict = message.data["event"]
                        event_id = event_dict["id"]
                        event_status = event_dict["status"]

                        current_tmsp = parser.parse(message.timestamp)

                        if (
                            message.type == "event-created"
                            and event_id not in ws.events_predictions
                        ):
                            if event_status == "ACTIVE":
                                prediction_window_seconds = float(
                                    event_dict["prediction_window_seconds"]
                                )
                                prediction_window_seconds -= (
                                    25 if prediction_window_seconds <= 180 else 60
                                )
                                event = EventPrediction(
                                    ws.streamers[streamer_index],
                                    event_id,
                                    event_dict["title"],
                                    parser.parse(event_dict["created_at"]),
                                    prediction_window_seconds,
                                    event_status,
                                    event_dict["outcomes"],
                                )
                                if (
                                    ws.streamers[streamer_index].is_online
                                    and event.closing_bet_after(current_tmsp) > 0
                                    and bet_condition(
                                        ws.browser,
                                        event,
                                        logger,
                                    )
                                    is True
                                ):
                                    ws.events_predictions[event_id] = event
                                    (
                                        start_bet_status,
                                        execution_time,
                                    ) = ws.browser.start_bet(
                                        ws.events_predictions[event_id]
                                    )
                                    if start_bet_status is True:
                                        # place_bet_thread = threading.Timer(event.closing_bet_after(current_tmsp), ws.twitch.make_predictions, (ws.events_predictions[event_id],))
                                        start_after = calculate_start_after(
                                            event.closing_bet_after(current_tmsp),
                                            execution_time,
                                        )

                                        place_bet_thread = threading.Timer(
                                            start_after,
                                            ws.browser.place_bet,
                                            (ws.events_predictions[event_id],),
                                        )
                                        place_bet_thread.daemon = True
                                        place_bet_thread.start()

                                        logger.info(
                                            f"Place the bet after: {start_after}s for: {ws.events_predictions[event_id]}",
                                            extra={"emoji": ":alarm_clock:"},
                                        )
                                    else:
                                        del ws.events_predictions[event_id]

                        elif (
                            message.type == "event-updated"
                            and event_id in ws.events_predictions
                        ):
                            ws.events_predictions[event_id].status = event_status
                            # Game over we can't update anymore the values... The bet was placed!
                            if (
                                ws.events_predictions[event_id].bet_placed is False
                                and ws.events_predictions[event_id].bet.decision == {}
                            ):
                                ws.events_predictions[event_id].bet.update_outcomes(
                                    event_dict["outcomes"]
                                )

                    elif message.topic == "predictions-user-v1":
                        event_id = message.data["prediction"]["event_id"]
                        if event_id in ws.events_predictions:
                            if message.type == "prediction-result":
                                event_result = message.data["prediction"]["result"]
                                logger.info(
                                    f"{ws.events_predictions[event_id]} - Result: {event_result['type']}, Points won: {_millify(event_result['points_won']) if event_result['points_won'] else 0}",
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
                            elif message.type == "prediction-made":
                                ws.events_predictions[event_id].bet_confirmed = True

                    elif message.topic == "user-drop-events":
                        if message.type == "drop-progress":
                            current = message.data["current_progress_min"]
                            required = message.data["required_progress_min"]
                            if current >= required:
                                try:
                                    drop = ws.twitch.search_drop_in_inventory(
                                        ws.streamers[streamer_index],
                                        message.data["drop_id"],
                                    )
                                    if drop["dropInstanceID"] is not None:
                                        ws.twitch.claim_drop(
                                            drop["dropInstanceID"],
                                            ws.streamers[streamer_index],
                                        )
                                except TimeBasedDropNotFound:
                                    logger.error(
                                        f"Unable to find {message.data['drop_id']} in your inventory"
                                    )
                            else:
                                # Skip 0% and 100% ...
                                percentage_state = int((current / required) * 100)
                                if percentage_state != 0 and percentage_state % 25 == 0:
                                    logger.info(
                                        f"Drop event {percentage_state}% for {ws.streamers[streamer_index]}!",
                                        extra={"emoji": ":package:"},
                                    )

                except Exception:
                    logger.error(
                        f"Exception raised for topic: {message.topic} and message: {message}",
                        exc_info=True,
                    )

        elif response["type"] == "RESPONSE" and len(response.get("error", "")) > 0:
            raise RuntimeError(f"Error while trying to listen for a topic: {response}")

        elif response["type"] == "RECONNECT":
            logger.info(f"#{ws.index} - Reconnection required")
            ws.is_reconneting = True
            WebSocketsPool.handle_reconnection(ws)

        elif response["type"] == "PONG":
            ws.last_pong = time.time()
