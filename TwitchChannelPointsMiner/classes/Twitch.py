# For documentation on Twitch GraphQL API see:
# https://www.apollographql.com/docs/
# https://github.com/mauricew/twitch-graphql-api
# Full list of available methods: https://azr.ivr.fi/schema/query.doc.html (a bit outdated)


import copy
import logging
import os
import random
import re
import time
from pathlib import Path
from secrets import token_hex

import requests

from TwitchChannelPointsMiner.classes.entities.Campaign import Campaign
from TwitchChannelPointsMiner.classes.Exceptions import (
    StreamerDoesNotExistException,
    StreamerIsOfflineException,
    TimeBasedDropNotFound,
)
from TwitchChannelPointsMiner.classes.Settings import Priority, Settings
from TwitchChannelPointsMiner.classes.TwitchLogin import TwitchLogin
from TwitchChannelPointsMiner.constants import API, CLIENT_ID, GQLOperations
from TwitchChannelPointsMiner.utils import _millify

logger = logging.getLogger(__name__)


class Twitch(object):
    def __init__(self, username, user_agent):
        cookies_path = os.path.join(Path().absolute(), "cookies")
        Path(cookies_path).mkdir(parents=True, exist_ok=True)
        self.cookies_file = os.path.join(cookies_path, f"{username}.pkl")
        self.user_agent = user_agent
        self.twitch_login = TwitchLogin(CLIENT_ID, username, self.user_agent)
        self.running = True

    def login(self):
        if os.path.isfile(self.cookies_file) is False:
            if self.twitch_login.login_flow():
                self.twitch_login.save_cookies(self.cookies_file)
        else:
            self.twitch_login.load_cookies(self.cookies_file)
            self.twitch_login.set_token(self.twitch_login.get_auth_token())

    def update_stream(self, streamer):
        if streamer.stream.update_required() is True:
            stream_info = self.get_stream_info(streamer)
            streamer.stream.update(
                broadcast_id=stream_info["stream"]["id"],
                title=stream_info["broadcastSettings"]["title"],
                game=stream_info["broadcastSettings"]["game"],
                tags=stream_info["stream"]["tags"],
                viewers_count=stream_info["stream"]["viewersCount"],
            )

            event_properties = {
                "channel_id": streamer.channel_id,
                "broadcast_id": streamer.stream.broadcast_id,
                "player": "site",
                "user_id": self.twitch_login.get_user_id(),
            }

            if (
                streamer.stream.game_name() is not None
                and streamer.settings.claim_drops is True
            ):
                event_properties["game"] = streamer.stream.game_name()

            streamer.stream.payload = [
                {"event": "minute-watched", "properties": event_properties}
            ]

    def get_spade_url(self, streamer):
        headers = {"User-Agent": self.user_agent}
        main_page_request = requests.get(streamer.streamer_url, headers=headers)
        response = main_page_request.text
        settings_url = re.search(
            "(https://static.twitchcdn.net/config/settings.*?js)", response
        ).group(1)

        settings_request = requests.get(settings_url, headers=headers)
        response = settings_request.text
        streamer.stream.spade_url = re.search('"spade_url":"(.*?)"', response).group(1)

    def post_gql_request(self, json_data):
        response = requests.post(
            GQLOperations.url,
            json=json_data,
            headers={
                "Authorization": f"OAuth {self.twitch_login.get_auth_token()}",
                "Client-Id": CLIENT_ID,
                "User-Agent": self.user_agent,
            },
        )
        logger.debug(
            f"Data: {json_data}, Status code: {response.status_code}, Content: {response.text}"
        )
        return response.json()

    def get_broadcast_id(self, streamer):
        json_data = copy.deepcopy(GQLOperations.WithIsStreamLiveQuery)
        json_data["variables"] = {"id": streamer.channel_id}
        response = self.post_gql_request(json_data)
        stream = response["data"]["user"]["stream"]
        if stream is not None:
            return stream["id"]
        else:
            raise StreamerIsOfflineException

    def get_stream_info(self, streamer):
        json_data = copy.deepcopy(GQLOperations.VideoPlayerStreamInfoOverlayChannel)
        json_data["variables"] = {"channel": streamer.username}
        response = self.post_gql_request(json_data)
        if response["data"]["user"]["stream"] is None:
            raise StreamerIsOfflineException
        else:
            return response["data"]["user"]

    def check_streamer_online(self, streamer):
        if time.time() < streamer.offline_at + 60:
            return

        if streamer.is_online is False:
            try:
                self.get_spade_url(streamer)
                self.update_stream(streamer)
            except StreamerIsOfflineException:
                streamer.set_offline()
            else:
                streamer.set_online()
        else:
            try:
                self.update_stream(streamer)
            except StreamerIsOfflineException:
                streamer.set_offline()

    def claim_bonus(self, streamer, claim_id):
        if Settings.logger.less is False:
            logger.info(
                f"Claiming the bonus for {streamer}!", extra={"emoji": ":gift:"}
            )

        json_data = copy.deepcopy(GQLOperations.ClaimCommunityPoints)
        json_data["variables"] = {
            "input": {"channelID": streamer.channel_id, "claimID": claim_id}
        }
        self.post_gql_request(json_data)

    def claim_drop(self, drop):
        logger.info(f"Claim {drop}", extra={"emoji": ":package:"})

        json_data = copy.deepcopy(GQLOperations.DropsPage_ClaimDropRewards)
        json_data["variables"] = {"input": {"dropInstanceID": drop.drop_instance_id}}
        self.post_gql_request(json_data)

    def search_drop_in_inventory(self, streamer, drop_id):
        inventory = self.__get_inventory()
        for campaign in inventory["dropCampaignsInProgress"]:
            for drop in campaign["timeBasedDrops"]:
                if drop["id"] == drop_id:
                    return drop["self"]
        raise TimeBasedDropNotFound

    def claim_all_drops_from_inventory(self):
        inventory = self.__get_inventory()
        for campaign in inventory["dropCampaignsInProgress"]:
            for drop in campaign["timeBasedDrops"]:
                if drop["self"]["dropInstanceID"] is not None:
                    self.claim_drop(drop["self"]["dropInstanceID"])
                    time.sleep(random.uniform(5, 10))

    def __get_inventory(self):
        response = self.post_gql_request(GQLOperations.Inventory)
        return response["data"]["currentUser"]["inventory"]

    def __get_drops_dashboard(self, status=None):
        response = self.post_gql_request(GQLOperations.ViewerDropsDashboard)
        campaigns = response["data"]["currentUser"]["dropCampaigns"]
        if status is not None:
            campaigns = [camp for camp in campaigns if camp["status"] == status.upper()]
        return campaigns

    # I'm not sure that this method It's fully working. We don't need it for the moment
    def __get_campaigns_details(self, campaigns):
        json_data = []
        for campaign in campaigns:
            json_data.append(copy.deepcopy(GQLOperations.DropCampaignDetails))
            json_data[-1]["variables"] = {
                "dropID": campaign["id"],
                "channelLogin": f"{self.twitch_login.get_user_id()}",
            }

        response = self.post_gql_request(json_data)
        return [res["data"]["user"]["dropCampaign"] for res in response]

    def sync_drops_campaigns(self, streamers):
        campaigns_update = 0
        while self.running:
            # Get update from dashboard each 30minutes
            if campaigns_update == 0 or ((time.time() - campaigns_update) / 60) > 30:
                campaigns_details = self.__get_campaigns_details(
                    self.__get_drops_dashboard(status="ACTIVE")
                )
                campaigns = []

                # Going to clear array and structure. Remove all the timeBasedDrops expired or not started yet
                for index in range(0, len(campaigns_details)):
                    campaign = Campaign(campaigns_details[index])
                    if campaign.dt_match is True:
                        # Remove all the drops already claimed or with dt not matching
                        campaign.clear_drops()
                        if campaign.drops != []:
                            campaigns.append(campaign)

            # Get data from inventory and sync current status with streamers.drops_campaigns
            inventory = self.__get_inventory()
            # Iterate all campaigns from dashboard (only active, with working drops)
            # In this array we have also the campaigns never started from us (not in nventory)
            for i in range(0, len(campaigns)):
                # Iterate all campaigns currently in progress from out inventory
                for in_progress in inventory["dropCampaignsInProgress"]:
                    if in_progress["id"] == campaigns[i].id:
                        campaigns[i].in_inventory = True
                        logger.info(campaigns[i])
                        # Iterate all the drops from inventory
                        for drop in in_progress["timeBasedDrops"]:
                            # Iterate all the drops from out campaigns array
                            # After id match update with
                            #   - currentMinutesWatched
                            #   - hasPreconditionsMet
                            #   - dropInstanceID
                            #   - isClaimed
                            for j in range(0, len(campaigns[i].drops)):
                                current_id = campaigns[i].drops[j].id
                                if drop["id"] == current_id:
                                    campaigns[i].drops[j].update(drop["self"])
                                    # If after update we all conditions are meet we can claim the drop
                                    if campaigns[i].drops[j].is_claimable is True:
                                        self.claim_drop(campaigns[i].drops[j])
                                    logger.info(campaigns[i].drops[j])
                                    break  # Found it!
                        break  # Found it!

            """
            campaign_not_started = [
                campaign for campaign in campaigns if campaign.in_inventory is False
            ]
            logger.info(
                f"We could start all of this campaigns: {len(campaign_not_started)}"
            )
            logger.info(
                f"Campaign active in dashboard: {len(campaigns)}, in progress on our inventory: {len(inventory['dropCampaignsInProgress'])}"
            )
            """

            # Check if user It's currently streaming the same game present in campaigns_details
            for index in range(0, len(streamers)):
                if (
                    streamers[index].settings.claim_drops is True
                    and streamers[index].is_online is True
                    and streamers[index].stream.drops_tags is True
                ):
                    streamers[index].stream.drops_campaigns = []
                    # yes! The streamer[index] have the drops_tags enabled and we It's currently stream a game with campaign active!
                    for campaign in campaigns:
                        if campaign.game == streamers[index].stream.game:
                            streamers[index].stream.drops_campaigns.append(campaign)

            time.sleep(60)

    # Load the amount of current points for a channel, check if a bonus is available
    def load_channel_points_context(self, streamer):
        json_data = copy.deepcopy(GQLOperations.ChannelPointsContext)
        json_data["variables"] = {"channelLogin": streamer.username}

        response = self.post_gql_request(json_data)
        if response["data"]["community"] is None:
            raise StreamerDoesNotExistException
        channel = response["data"]["community"]["channel"]
        community_points = channel["self"]["communityPoints"]
        streamer.channel_points = community_points["balance"]

        if community_points["availableClaim"] is not None:
            self.claim_bonus(streamer, community_points["availableClaim"]["id"])

    def make_predictions(self, event):
        decision = event.bet.calculate(event.streamer.channel_points)
        selector_index = 0 if decision["choice"] == "A" else 1

        logger.info(
            f"Going to complete bet for {event}",
            extra={"emoji": ":four_leaf_clover:"},
        )
        if event.status == "ACTIVE":
            skip, compared_value = event.bet.skip()
            if skip is True:
                logger.info(
                    f"Skip betting for the event {event}", extra={"emoji": ":pushpin:"}
                )
                logger.info(
                    f"Skip settings {event.bet.settings.filter_condition}, current value is: {compared_value}",
                    extra={"emoji": ":pushpin:"},
                )
            else:
                logger.info(
                    f"Place {_millify(decision['amount'])} channel points on: {event.bet.get_outcome(selector_index)}",
                    extra={"emoji": ":four_leaf_clover:"},
                )

                json_data = copy.deepcopy(GQLOperations.MakePrediction)
                json_data["variables"] = {
                    "input": {
                        "eventID": event.event_id,
                        "outcomeID": decision["id"],
                        "points": decision["amount"],
                        "transactionID": token_hex(16),
                    }
                }
                return self.post_gql_request(json_data)
        else:
            logger.info(
                f"Oh no! The event is not active anymore! Current status: {event.status}",
                extra={"emoji": ":disappointed_relieved:"},
            )

    def send_minute_watched_events(self, streamers, priority, chunk_size=3):
        while self.running:
            # OK! We will do the following:
            #   - Create an array of int - index of streamers currently online
            #   - Create a dictionary with grouped streamers, based on watch-streak or drops
            #   - For each array we don't need more than 2 streamer (becuase we can't watch more than 2)

            streamers_index = [
                i
                for i in range(0, len(streamers))
                if streamers[i].is_online
                and (
                    streamers[i].online_at == 0
                    or (time.time() - streamers[i].online_at) > 30
                )
            ]

            streamers_watching = []
            for prior in priority:
                if prior == Priority.ORDER and len(streamers_watching) <= 2:
                    # Get the first 2 items, they are already in order
                    streamers_watching.append(streamers_index[:2])
                elif prior == Priority.STREAK and len(streamers_watching) <= 2:
                    """
                    Check if we need need to change priority based on watch streak
                    Viewers receive points for returning for x consecutive streams.
                    Each stream must be at least 10 minutes long and it must have been at least 30 minutes since the last stream ended.

                    Watch at least 6m for get the +10
                    """
                    for index in streamers_index:
                        if (
                            streamers[index].settings.watch_streak is True
                            and streamers[index].stream.watch_streak_missing is True
                            and (
                                streamers[index].offline_at == 0
                                or ((time.time() - streamers[index].offline_at) // 60)
                                > 30
                            )
                            and streamers[index].stream.minute_watched < 7
                        ):
                            logger.debug(
                                f"Switch priority: {streamers[index]}, WatchStreak missing is {streamers[index].stream.watch_streak_missing} and minute_watched: {round(streamers[index].stream.minute_watched, 2)}"
                            )
                            streamers_watching.append(index)
                            if len(streamers_watching) == 2:
                                break

                elif prior == Priority.DROPS and len(streamers_watching) <= 2:
                    for index in streamers_index:
                        # For the truth we don't need al of this If - condition
                        # because the drops_campaigns can be fulled only if claim_drops is True and drops_tags is True
                        if (
                            streamers[index].settings.claim_drops is True
                            and streamers[index].stream.drops_tags is True
                            and streamers[index].stream.drops_campaigns != []
                        ):
                            drops_available = sum(
                                [
                                    len(campaign.drops)
                                    for campaign in streamers[
                                        index
                                    ].stream.drops_campaigns
                                ]
                            )
                            logger.debug(
                                f"{streamers[index]} it's currenty stream: {streamers[index].stream}"
                            )
                            logger.debug(
                                f"Campaign currently active here: {len(streamers[index].stream.drops_campaigns)}, drops available: {drops_available}"
                            )
                            streamers_watching.append(index)
                            if len(streamers_watching) == 2:
                                break

            """
            Twitch has a limit - you can't watch more than 2 channels at one time.
            We take the first two streamers from the list as they have the highest priority (based on order or WatchStreak).
            """
            streamers_watching = streamers_watching[:2]

            for index in streamers_watching:
                next_iteration = time.time() + 60 / len(streamers_watching)

                try:
                    response = requests.post(
                        streamers[index].stream.spade_url,
                        data=streamers[index].stream.encode_payload(),
                        headers={"User-Agent": self.user_agent},
                    )
                    logger.debug(
                        f"Send minute watched request for {streamers[index]} - Status code: {response.status_code}"
                    )
                    if response.status_code == 204:
                        streamers[index].stream.update_minute_watched()
                except requests.exceptions.ConnectionError as e:
                    logger.error(f"Error while trying to watch a minute: {e}")

                # Create chunk of sleep of speed-up the break loop after CTRL+C
                sleep_time = max(next_iteration - time.time(), 0) / chunk_size
                for i in range(0, chunk_size):
                    time.sleep(sleep_time)
                    if self.running is False:
                        break

            if streamers_watching == []:
                time.sleep(60)

    def get_channel_id(self, streamer_username):
        json_response = self.__do_helix_request(f"/users?login={streamer_username}")
        data = json_response["data"]
        if len(data) >= 1:
            return data[0]["id"]
        else:
            raise StreamerDoesNotExistException

    def get_followers(self, first=100):
        followers = []
        pagination = {}
        while 1:
            query = f"/users/follows?from_id={self.twitch_login.get_user_id()}&first={first}"
            if pagination != {}:
                query += f"&after={pagination['cursor']}"

            json_response = self.__do_helix_request(query)
            pagination = json_response["pagination"]
            followers += [fw["to_name"].lower() for fw in json_response["data"]]
            time.sleep(random.uniform(0.3, 0.7))

            if pagination == {}:
                break

        return followers

    def __do_helix_request(self, query, response_as_json=True):
        url = f"{API}/helix/{query.strip('/')}"
        response = self.twitch_login.session.get(url)
        logger.debug(
            f"Query: {query}, Status code: {response.status_code}, Content: {response.json()}"
        )
        return response.json() if response_as_json is True else response

    def update_raid(self, streamer, raid):
        if streamer.raid != raid:
            streamer.raid = raid
            json_data = copy.deepcopy(GQLOperations.JoinRaid)
            json_data["variables"] = {"input": {"raidID": raid.raid_id}}
            self.post_gql_request(json_data)

            logger.info(
                f"Joining raid from {streamer} to {raid.target_login}!",
                extra={"emoji": ":performing_arts:"},
            )

    def viewer_is_mod(self, streamer):
        json_data = copy.deepcopy(GQLOperations.ModViewChannelQuery)
        json_data["variables"] = {"channelLogin": streamer.username}
        response = self.post_gql_request(json_data)
        try:
            streamer.viewer_is_mod = response["data"]["user"]["self"]["isModerator"]
        except (ValueError, KeyError):
            streamer.viewer_is_mod = False
