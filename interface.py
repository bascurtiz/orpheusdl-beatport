import logging
import re

from datetime import datetime

from utils.models import *
from utils.models import AlbumInfo
from .beatport_api import BeatportApi, BeatportError

module_information = ModuleInformation(
    service_name="Beatport",
    module_supported_modes=ModuleModes.download | ModuleModes.covers,
    login_behaviour=ManualEnum.manual,
    session_settings={"username": "", "password": ""},
    session_storage_variables=["access_token", "refresh_token", "expires"],
    netlocation_constant="beatport",
    url_decoding=ManualEnum.manual,
    test_url="https://www.beatport.com/track/darkside/10844269"
)


class ModuleInterface:
    # noinspection PyTypeChecker
    def __init__(self, module_controller: ModuleController):
        self.exception = module_controller.module_error
        self.disable_subscription_check = module_controller.orpheus_options.disable_subscription_check
        self.oprinter = module_controller.printer_controller
        self.print = module_controller.printer_controller.oprint
        self.module_controller = module_controller
        self.cover_size = module_controller.orpheus_options.default_cover_options.resolution

        # MINIMUM-MEDIUM = 128kbit/s AAC, HIGH = 256kbit/s AAC, LOSSLESS-HIFI = FLAC 44.1/16
        self.quality_parse = {
            QualityEnum.MINIMUM: "medium",
            QualityEnum.LOW: "medium",
            QualityEnum.MEDIUM: "medium",
            QualityEnum.HIGH: "medium",
            QualityEnum.LOSSLESS: "medium",
            QualityEnum.HIFI: "medium"
        }

        self.session = BeatportApi()
        session = {
            "access_token": module_controller.temporary_settings_controller.read("access_token"),
            "refresh_token": module_controller.temporary_settings_controller.read("refresh_token"),
            "expires": module_controller.temporary_settings_controller.read("expires")
        }

        self.session.set_session(session)

        if session["refresh_token"] is None:
            # old beatport version with cookies and no refresh token, trigger login manually
            session = self.login(module_controller.module_settings["username"],
                                 module_controller.module_settings["password"])

        if session["refresh_token"] is not None and datetime.now() > session["expires"]:
            # access token expired, get new refresh token
            self.refresh_login()

        self.valid_account()

    def _save_session(self) -> dict:
        # save the new access_token, refresh_token and expires in the temporary settings
        self.module_controller.temporary_settings_controller.set("access_token", self.session.access_token)
        self.module_controller.temporary_settings_controller.set("refresh_token", self.session.refresh_token)
        self.module_controller.temporary_settings_controller.set("expires", self.session.expires)

        return {
            "access_token": self.session.access_token,
            "refresh_token": self.session.refresh_token,
            "expires": self.session.expires
        }

    def refresh_login(self):
        logging.debug(f"Beatport: access_token expired, getting a new one")

        # get a new access_token and refresh_token from the API
        refresh_data = self.session.refresh()
        if refresh_data and refresh_data.get("error") == "invalid_grant":
            # if the refresh token is invalid, trigger login
            self.login(self.module_controller.module_settings["username"],
                       self.module_controller.module_settings["password"])
            return

        self._save_session()
            
    def login(self, email: str, password: str):
        logging.debug(f"Beatport: no session found, login")
        login_data = self.session.auth(email, password)

        if login_data.get("error_description") is not None:
            raise self.exception(login_data.get("error_description"))

        self.valid_account()

        return self._save_session()

    def valid_account(self):
        if not self.disable_subscription_check:
            # get the subscription from the API and check if it's at least a "Link" subscription
            account_data = self.session.get_account()
            if not account_data.get("subscription"):
                raise self.exception("Beatport: Account does not have an active 'Link' subscription")

            # Essentials = "bp_basic", Professional = "bp_link_pro"
            if account_data.get("subscription") == "bp_link_pro":
                # Pro subscription, set the quality to high and lossless
                self.print("Beatport: Professional subscription detected, allowing high and lossless quality")
                self.quality_parse[QualityEnum.HIGH] = "high"
                self.quality_parse[QualityEnum.HIFI] = "lossless"
                self.quality_parse[QualityEnum.LOSSLESS] = "lossless"

    @staticmethod
    def custom_url_parse(link: str):
        # Previous regex (caused issues): r"https?://(www.)?beatport.com/(?:[a-z]{2}/)?.*?/(?P<type>track|release|artist|playlists|chart)/.*/?(?P<id>\d+)"
        # New, more explicit regex:
        match = re.search(r"https?://(www\.)?beatport\.com/(?:[a-z]{2}/)?(?P<type>track|release|artist|playlists|chart)/(?P<slug>.+)/(?P<id>\d+)", link)

        # so parse the regex "match" to the actual DownloadTypeEnum
        media_types = {
            "track": DownloadTypeEnum.track,
            "release": DownloadTypeEnum.album,
            "artist": DownloadTypeEnum.artist,
            "playlists": DownloadTypeEnum.playlist,
            "chart": DownloadTypeEnum.playlist
        }

        if not match: # Added error handling for robustness
            raise ValueError(f"Could not parse Beatport URL: {link}")

        return MediaIdentification(
            media_type=media_types[match.group("type")],
            media_id=match.group("id"),
            # check if the playlist is a user playlist or DJ charts, only needed for get_playlist_info()
            extra_kwargs={"is_chart": match.group("type") == "chart"}
        )

    @staticmethod
    def _generate_artwork_url(cover_url: str, size: int, max_size: int = 1400):
        # if more than max_size are requested, cap the size at max_size
        if size > max_size:
            size = max_size

        # check if it"s a dynamic_uri, if not make it one
        res_pattern = re.compile(r"\d{3,4}x\d{3,4}")
        match = re.search(res_pattern, cover_url)
        if match:
            # replace the hardcoded resolution with dynamic one
            cover_url = re.sub(res_pattern, "{w}x{h}", cover_url)

        # replace the dynamic_uri h and w parameter with the wanted size
        return cover_url.format(w=size, h=size)

    def search(self, query_type: DownloadTypeEnum, query: str, track_info: TrackInfo = None, limit: int = 20):
        # map query types to API search types
        search_types = {
            DownloadTypeEnum.track: "tracks",
            DownloadTypeEnum.album: "releases",
            DownloadTypeEnum.playlist: "charts",
            DownloadTypeEnum.artist: "artists"
        }
        
        search_type = search_types.get(query_type)
        
        # perform search with type if supported, otherwise fall back to general search
        if search_type:
            results = self.session.get_search(query=query, search_type=search_type, per_page=limit)
            result_list = results.get(search_type, [])
        else:
            # fall back to general search for unsupported types
            results = self.session.get_search(query)
            name_parse = {
                "track": "tracks",
                "album": "releases",
                "playlist": "charts",
                "artist": "artists"
            }
            result_list = results.get(name_parse.get(query_type.name), [])
        
        items = []
        for i in result_list:
            # Initialize fields for SearchResult
            name = i.get('name', '')
            artists = []
            year = None
            duration = None
            additional = []
            item_extra_kwargs = {}
            image_uri = i.get('image', {}).get('uri')
            image_url = self._generate_artwork_url(image_uri, 500) if image_uri else None
            result_id = str(i.get('id'))
            is_explicit = i.get('explicit', False)

            if query_type is DownloadTypeEnum.playlist:
                item_extra_kwargs['is_chart'] = True # Beatport search for playlists returns charts
                # Artist parsing for charts
                if i.get('artist') and i['artist'].get('name'):
                    artists = [i['artist']['name']]
                elif i.get('person') and i['person'].get('owner_name'): # Fallback for different structures
                    artists = [i['person']['owner_name']]
                else:
                    artists = ["Beatport"] # Default
                # Year parsing for charts
                if i.get("publish_date"):
                    year = i.get("publish_date")[:4]
                elif i.get("change_date"): # Fallback date field
                    year = i.get("change_date")[:4]

            elif query_type is DownloadTypeEnum.track:
                artists = [a.get("name") for a in i.get("artists", [])]
                if i.get("publish_date"):
                    year = i.get("publish_date")[:4]
                if i.get("length_ms"):
                    duration = i.get("length_ms") // 1000
                if i.get("bpm"):
                    additional.append(f"{i.get('bpm')}BPM")
                if i.get("mix_name") and name: # Add mix name to track name
                    name += f" ({i.get('mix_name')})"

            elif query_type is DownloadTypeEnum.album:
                artists = [a.get("name") for a in i.get("artists", [])]
                if i.get("publish_date"):
                    year = i.get("publish_date")[:4]
                if i.get("catalog_number"):
                    additional.append(f"Cat: {i.get('catalog_number')}")
            
            elif query_type is DownloadTypeEnum.artist:
                if i.get("name"):
                    artists = [i.get("name")]
                # Year is usually not applicable for artist search results directly
                if i.get("genres"):
                    genre_names = [g.get("name") for g in i.get("genres", []) if g.get("name")]
                    if genre_names:
                        additional.append(", ".join(genre_names))

            if i.get("exclusive") is True:
                 additional.append("Exclusive")

            items.append(SearchResult(
                name=name,
                artists=artists if artists else ["Unknown Artist"], # Ensure artists list is not empty
                result_id=result_id,
                year=year,
                additional=additional if additional else None,
                duration=duration,
                explicit=is_explicit,
                extra_kwargs=item_extra_kwargs if item_extra_kwargs else {}
            ))
        return items
        
    def get_playlist_info(self, playlist_id: str, is_chart: bool = False) -> PlaylistInfo:
        all_tracks_raw = []
        current_page = 1
        per_page = 100 # Max items per page Beatport API usually allows for tracks

        if is_chart:
            playlist_data = self.session.get_chart(playlist_id)
            # Initial fetch for chart tracks
            tracks_page_data = self.session.get_chart_tracks(playlist_id, page=current_page, per_page=per_page)
        else:
            playlist_data = self.session.get_playlist(playlist_id)
            # Initial fetch for playlist tracks
            tracks_page_data = self.session.get_playlist_tracks(playlist_id, page=current_page, per_page=per_page)

        if tracks_page_data and 'results' in tracks_page_data:
            all_tracks_raw.extend(tracks_page_data['results'])
        
        total_items = tracks_page_data.get('count', 0) if tracks_page_data else 0
        
        # Paginate if necessary
        while len(all_tracks_raw) < total_items and total_items > 0:
            current_page += 1
            self.print(f"Fetching playlist/chart tracks page {current_page} ({len(all_tracks_raw)}/{total_items})")
            if is_chart:
                tracks_page_data = self.session.get_chart_tracks(playlist_id, page=current_page, per_page=per_page)
            else:
                tracks_page_data = self.session.get_playlist_tracks(playlist_id, page=current_page, per_page=per_page)
            
            if tracks_page_data and 'results' in tracks_page_data and tracks_page_data['results']:
                all_tracks_raw.extend(tracks_page_data['results'])
            else:
                # No more results or error, break loop
                logging.warning(f"Stopped pagination for {'chart' if is_chart else 'playlist'} {playlist_id} at page {current_page}. Expected {total_items}, got {len(all_tracks_raw)}.")
                break
        if total_items > 0: self.print("") # Clear the progress line by printing a newline

        # For playlists (non-charts), tracks are often nested under a 'track' key.
        # For charts, the track data is usually direct.
        if not is_chart:
             processed_tracks_ids = [str(track_item['track']['id']) for track_item in all_tracks_raw if 'track' in track_item and 'id' in track_item['track']]
        else: # For charts
             processed_tracks_ids = [str(track_item['id']) for track_item in all_tracks_raw if 'id' in track_item]


        # Common fields for both charts and playlists
        name = playlist_data.get('name', 'Unknown Playlist')
        description = playlist_data.get('description', '')
        
        # Fields might differ between chart and playlist
        if is_chart:
            creator_name = playlist_data.get('curator_name')
            if not creator_name and playlist_data.get('artist'): # Charts might have an 'artist' as curator
                 creator_name = playlist_data.get('artist', {}).get('name', 'Beatport')
            elif not creator_name: # Fallback if no curator or artist name
                 creator_name = "Beatport"
            
            creator_id = str(playlist_data.get('artist', {}).get('id')) if playlist_data.get('artist') else None
            release_date_str = playlist_data.get('publish_date') # Charts use 'publish_date'
            image_data = playlist_data.get('image')
            num_tracks_from_api = playlist_data.get('track_count', len(processed_tracks_ids)) # Charts often have 'track_count'
            is_explicit = playlist_data.get('explicit', False)

        else: # For actual playlists (if distinct endpoint/structure exists and is used)
              # This part is more speculative as primary focus is charts based on search.
              # If Beatport API has distinct user playlists, structure might be like this:
            creator_name = playlist_data.get('user', {}).get('username', 'Unknown Creator') 
            creator_id = str(playlist_data.get('user', {}).get('id')) if playlist_data.get('user') else None
            release_date_str = playlist_data.get('created_at') # Or 'updated_at' for playlists
            image_data = playlist_data.get('image') # Structure might vary
            num_tracks_from_api = playlist_data.get('tracks_count', len(processed_tracks_ids)) # Or 'count'
            is_explicit = False # Playlists might not have a global explicit flag like albums/tracks.

        release_year = None
        if release_date_str:
            try:
                # Handle ISO format dates (e.g., "2023-04-01T15:00:00Z")
                release_year = datetime.fromisoformat(release_date_str.replace('Z', '+00:00')).year
            except ValueError:
                try: # Fallback for simpler date strings like "YYYY-MM-DD"
                    release_year = datetime.strptime(release_date_str.split('T')[0], '%Y-%m-%d').year
                except ValueError:
                    logging.warning(f"Could not parse release date for {'chart' if is_chart else 'playlist'} {playlist_id}: {release_date_str}")

        cover_uri = image_data.get('uri') if image_data else None
        cover_url = self._generate_artwork_url(cover_uri, self.cover_size) if cover_uri else None
        cover_type_str = image_data.get('extension', 'jpg').lower() if image_data and image_data.get('extension') else 'jpg'
        cover_type = ImageFileTypeEnum[cover_type_str] if cover_type_str in ImageFileTypeEnum.__members__ else ImageFileTypeEnum.jpg
        
        # Consistency check
        if num_tracks_from_api != len(processed_tracks_ids):
            logging.warning(f"Playlist/Chart {name} ({playlist_id}): Number of tracks from API ({num_tracks_from_api}) differs from successfully parsed tracks ({len(processed_tracks_ids)}).")

        # Calculate duration (sum of track durations if available, Beatport charts/playlists don't usually provide this directly)
        # This would require fetching individual track details, which is too slow here. So, duration remains None.
        total_duration_seconds = None

        return PlaylistInfo(
            name=name,
            creator=creator_name,
            creator_id=creator_id,
            description=description,
            duration=total_duration_seconds,
            release_year=release_year,
            cover_url=cover_url,
            cover_type=cover_type,
            tracks=processed_tracks_ids,
            explicit=is_explicit,
            track_extra_kwargs={'is_chart': is_chart} # Pass is_chart down for track processing
        )

    def get_artist_info(self, artist_id: str, get_credited_albums: bool, is_chart: bool = False) -> ArtistInfo:
        artist_data = self.session.get_artist(artist_id)
        artist_tracks_data = self.session.get_artist_tracks(artist_id)

        # now fetch all the found total_items
        artist_tracks = artist_tracks_data.get("results")
        total_tracks = artist_tracks_data.get("count")
        for page in range(2, total_tracks // 100 + 2):
            print(f"Fetching {page * 100}/{total_tracks}", end="\r")
            artist_tracks += self.session.get_artist_tracks(artist_id, page=page).get("results")

        return ArtistInfo(
            name=artist_data.get("name"),
            tracks=[t.get("id") for t in artist_tracks],
            track_extra_kwargs={"data": {t.get("id"): t for t in artist_tracks}},
        )

    def get_album_info(self, album_id: str, data=None, is_chart: bool = False) -> AlbumInfo or None:
        # check if album is already in album cache, add it
        if data is None:
            data = {}

        try:
            album_data = data.get(album_id) if album_id in data else self.session.get_release(album_id)
        except BeatportError as e:
            self.print(f"Beatport: Album {album_id} is {str(e)}")
            return

        tracks_data = self.session.get_release_tracks(album_id)

        # now fetch all the found total_items
        tracks = tracks_data.get("results")
        total_tracks = tracks_data.get("count")
        for page in range(2, total_tracks // 100 + 2):
            print(f"Fetching {len(tracks)}/{total_tracks}", end="\r")
            tracks += self.session.get_release_tracks(album_id, page=page).get("results")

        cache = {"data": {album_id: album_data}}
        for i, track in enumerate(tracks):
            # add the track numbers
            track["number"] = i + 1
            # add the modified track to the track_extra_kwargs
            cache["data"][track.get("id")] = track

        return AlbumInfo(
            name=album_data.get("name"),
            release_year=album_data.get("publish_date")[:4] if album_data.get("publish_date") else None,
            # sum up all the individual track lengths
            duration=sum([(t.get("length_ms") or 0) // 1000 for t in tracks]),
            upc=album_data.get("upc"),
            cover_url=self._generate_artwork_url(album_data.get("image").get("dynamic_uri"), self.cover_size),
            artist=album_data.get("artists")[0].get("name"),
            artist_id=album_data.get("artists")[0].get("id"),
            tracks=[t.get("id") for t in tracks],
            track_extra_kwargs=cache,
        )

    def get_track_info(self, track_id: str, quality_tier: QualityEnum, codec_options: CodecOptions, slug: str = None,
                       data=None, is_chart: bool = False) -> TrackInfo:
        if data is None:
            data = {}

        track_data = data[track_id] if track_id in data else self.session.get_track(track_id)

        album_id = track_data.get("release").get("id")
        album_data = {}
        error = None

        try:
            album_data = data[album_id] if album_id in data else self.session.get_release(album_id)
        except ConnectionError as e:
            # check if the album is region locked
            if "Territory Restricted." in str(e):
                error = f"Album {album_id} is region locked"

        track_name = track_data.get("name")
        track_name += f" ({track_data.get('mix_name')})" if track_data.get("mix_name") else ""

        release_year = track_data.get("publish_date")[:4] if track_data.get("publish_date") else None
        genres = [track_data.get("genre").get("name")]
        # check if a second genre exists
        genres += [track_data.get("sub_genre").get("name")] if track_data.get("sub_genre") else []

        extra_tags = {}
        if track_data.get("bpm"):
            extra_tags["BPM"] = str(track_data.get("bpm"))
        if track_data.get("key"):
            extra_tags["Key"] = track_data.get("key").get("name")
        if track_data.get("catalog_number"):
            extra_tags["Catalog number"] = track_data.get("catalog_number")

        tags = Tags(
            album_artist=album_data.get("artists", [{}])[0].get("name"),
            track_number=track_data.get("number"),
            total_tracks=album_data.get("track_count"),
            upc=album_data.get("upc"),
            isrc=track_data.get("isrc"),
            genres=genres,
            release_date=track_data.get("publish_date"),
            copyright=f"© {release_year} {track_data.get('release').get('label').get('name')}",
            label=track_data.get("release").get("label").get("name"),
            extra_tags=extra_tags
        )

        if not track_data["is_available_for_streaming"]:
            error = f"Track '{track_data.get('name')}' is not streamable!"
        elif track_data.get("preorder"):
            error = f"Track '{track_data.get('name')}' is not yet released!"

        quality = self.quality_parse[quality_tier]
        bitrate = {
            "lossless": 1411,
            "high": 256,
            "medium": 128,
        }
        length_ms = track_data.get("length_ms")

        track_info = TrackInfo(
            name=track_name,
            album=album_data.get("name"),
            album_id=album_data.get("id"),
            artists=[a.get("name") for a in track_data.get("artists")],
            artist_id=track_data.get("artists")[0].get("id"),
            release_year=release_year,
            duration=length_ms // 1000 if length_ms else None,
            bitrate=bitrate[quality],
            bit_depth=16 if quality == "lossless" else None,  # https://en.wikipedia.org/wiki/Audio_bit_depth#cite_ref-1
            sample_rate=44.1,
            cover_url=self._generate_artwork_url(
                track_data.get("release").get("image").get("dynamic_uri"), self.cover_size),
            tags=tags,
            codec=CodecEnum.FLAC if quality == "lossless" else CodecEnum.AAC,
            download_extra_kwargs={"track_id": track_id, "quality_tier": quality_tier},
            error=error
        )

        return track_info

    def get_track_cover(self, track_id: str, cover_options: CoverOptions, data=None) -> CoverInfo:
        if data is None:
            data = {}

        track_data = data[track_id] if track_id in data else self.session.get_track(track_id)
        cover_url = track_data.get("release").get("image").get("dynamic_uri")

        return CoverInfo(
            url=self._generate_artwork_url(cover_url, cover_options.resolution),
            file_type=ImageFileTypeEnum.jpg)

    def get_track_download(self, track_id: str, quality_tier: QualityEnum) -> TrackDownloadInfo:
        stream_data = self.session.get_track_download(track_id, self.quality_parse[quality_tier])

        if not stream_data.get("location"):
            raise self.exception("Could not get stream, exiting")

        return TrackDownloadInfo(
            download_type=DownloadEnum.URL,
            file_url=stream_data.get("location")
        )