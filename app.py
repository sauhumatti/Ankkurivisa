import os
import random
import time
import re # Import the regex module
from datetime import datetime, timezone
from dateutil.parser import isoparse
from whitenoise import WhiteNoise
from dotenv import load_dotenv
from flask import Flask, redirect, request, session, url_for, render_template, jsonify, flash
from markupsafe import Markup # Needed for the |safe filter in template
from requests_oauthlib import OAuth2Session
from supabase import create_client, Client

# --- Configuration --- (Keep existing config)
load_dotenv()
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv("FLASK_SECRET_KEY", "default_fallback_secret_key")

# --- Supabase Client ---
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_KEY")
if not supabase_url or not supabase_key:
    raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in .env")
supabase: Client = create_client(supabase_url, supabase_key)

# --- Discord OAuth Config ---
discord_client_id = os.getenv("DISCORD_CLIENT_ID")
discord_client_secret = os.getenv("DISCORD_CLIENT_SECRET")
discord_redirect_uri = os.getenv("DISCORD_REDIRECT_URI")
discord_scope = ['identify']
discord_api_base_url = 'https://discord.com/api'
discord_authorization_base_url = f'{discord_api_base_url}/oauth2/authorize'
discord_token_url = f'{discord_api_base_url}/oauth2/token'

# --- Game Config ---  <- MAKE SURE THIS SECTION IS PRESENT
POSSIBLE_YEARS = list(range(2016, 2026)) # 2016 to 2025
POSSIBLE_CHANNELS = [
    "paskapuheet", "doto", "äijjienkanava", "weeb-games", "cheeki-breeki",
    "politiikka", "koodivelhot", "läskit-ja-rohkeat", "pokekanava",
    "civi", "stonks", "tarjoushaukat"
]
MIN_MESSAGE_LENGTH = 5
ELO_K_FACTOR = 32
BASE_POINTS_CORRECT = 15
BASE_POINTS_INCORRECT = -10
MAX_TIME_BONUS_SECONDS = 20
MAX_BONUS_POINTS = 10
# --- End Game Config ---

# --- Helper Functions --- (Keep existing: get_discord_oauth_session, fetch_discord_user, etc.)
# ... (get_discord_oauth_session, fetch_discord_user, get_or_create_user_profile, calculate_elo_change, update_user_elo, get_random_consecutive_messages) ...
def get_discord_oauth_session(state=None, token=None):
    return OAuth2Session(
        discord_client_id,
        redirect_uri=discord_redirect_uri,
        scope=discord_scope,
        state=state,
        token=token
    )

def fetch_discord_user(token):
    discord = get_discord_oauth_session(token=token)
    try:
        response = discord.get(f'{discord_api_base_url}/users/@me')
        response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
        user_data = response.json()
        # Combine username and discriminator if available
        username = user_data.get('username')
        discriminator = user_data.get('discriminator')
        full_username = f"{username}#{discriminator}" if discriminator and discriminator != "0" else username
        return {'id': user_data['id'], 'username': full_username}
    except Exception as e:
        app.logger.error(f"Error fetching Discord user: {e}")
        return None

def get_or_create_user_profile(discord_id, discord_username):
    try:
        # Check if user exists
        response = supabase.table("user_profiles").select("*").eq("discord_id", discord_id).limit(1).execute()

        if response.data:
            # User exists, update username if changed, return profile
            profile = response.data[0]
            if profile.get('discord_username') != discord_username:
                 supabase.table("user_profiles").update({"discord_username": discord_username}).eq("discord_id", discord_id).execute()
                 profile['discord_username'] = discord_username # Update local copy
            return profile
        else:
            # User doesn't exist, create them
            new_profile_data = {
                "discord_id": discord_id,
                "discord_username": discord_username,
                "elo_rating": 1000, # Starting ELO
                "games_played": 0
            }
            insert_response = supabase.table("user_profiles").insert(new_profile_data).execute()
            if insert_response.data:
                return insert_response.data[0]
            else:
                 app.logger.error(f"Failed to insert new user profile for {discord_id}")
                 return None # Indicate failure
    except Exception as e:
        app.logger.error(f"Database error getting/creating profile for {discord_id}: {e}")
        return None

def calculate_elo_change(correct, time_taken_seconds):
    """Calculates points change based on correctness and speed."""
    if correct:
        points = BASE_POINTS_CORRECT
        # Add time bonus (decays linearly from MAX_BONUS_POINTS to 0 over MAX_TIME_BONUS_SECONDS)
        if time_taken_seconds < MAX_TIME_BONUS_SECONDS:
            time_bonus = MAX_BONUS_POINTS * (1 - (time_taken_seconds / MAX_TIME_BONUS_SECONDS))
            points += round(time_bonus)
        return points
    else:
        return BASE_POINTS_INCORRECT

def update_user_elo(discord_id, year_correct, channel_correct, time_taken_seconds):
    """Updates user ELO based on game results."""
    try:
        profile_response = supabase.table("user_profiles").select("elo_rating, games_played").eq("discord_id", discord_id).single().execute()
        if not profile_response.data:
            app.logger.error(f"Could not find profile to update ELO for {discord_id}")
            return None, None # Indicate failure

        current_elo = profile_response.data['elo_rating']
        games_played = profile_response.data['games_played']

        year_elo_change = calculate_elo_change(year_correct, time_taken_seconds)
        channel_elo_change = calculate_elo_change(channel_correct, time_taken_seconds)
        total_elo_change = year_elo_change + channel_elo_change

        new_elo = current_elo + total_elo_change
        new_games_played = games_played + 1
        now_utc = datetime.now(timezone.utc).isoformat()

        update_response = supabase.table("user_profiles").update({
            "elo_rating": new_elo,
            "games_played": new_games_played,
            "last_played_at": now_utc
        }).eq("discord_id", discord_id).execute()

        if update_response.data:
             app.logger.info(f"Updated ELO for {discord_id}: {current_elo} -> {new_elo} (Change: {total_elo_change})")
             return total_elo_change, new_elo # Return change and new ELO
        else:
             app.logger.error(f"Failed to update ELO for {discord_id}. Response: {update_response.error}")
             return None, None

    except Exception as e:
        app.logger.error(f"Database error updating ELO for {discord_id}: {e}")
        return None, None

def get_random_consecutive_messages(count=3):
    """Fetches a random starting message and the next 'count-1' messages from the same channel."""
    try:
        # 1. Get random starting message ID via RPC
        random_start_response = supabase.rpc(
            'get_random_message_id',
             params={'min_length': MIN_MESSAGE_LENGTH} # Note: RPC function itself enforces min_length
        ).execute()

        # Simplified error handling/fallback
        if not random_start_response.data:
             app.logger.warning("RPC failed or returned no ID. Attempting simple fallback.")
             fallback_resp = supabase.table("discord_messages").select("id").order("id", desc=True).limit(1000).execute()
             if not fallback_resp.data:
                 app.logger.error("Fallback failed: No messages found.")
                 return None
             start_id = random.choice(fallback_resp.data)['id'] - random.randint(10, 500) # Guess older ID
        else:
             start_id = random_start_response.data

        app.logger.debug(f"Attempting fetch sequence starting near ID: {start_id}")

        # --- Select attachment_urls ---
        select_columns = "id, channel, sent_at, author_name, content, attachment_urls, has_attachments"

        # 2. Fetch the first message
        first_msg_response = supabase.table("discord_messages") \
            .select(select_columns) \
            .gte("id", start_id) \
            .order("id", desc=False) \
            .limit(1) \
            .execute()

        if not first_msg_response.data:
            app.logger.warning(f"Could not fetch initial message near ID {start_id}")
            return None

        first_message = first_msg_response.data[0]
        start_channel = first_message['channel']
        start_sent_at = first_message['sent_at']

        # 3. Fetch subsequent messages
        subsequent_msgs_response = supabase.table("discord_messages") \
            .select(select_columns) \
            .eq("channel", start_channel) \
            .gt("sent_at", start_sent_at) \
            .order("sent_at", desc=False) \
            .limit(count - 1) \
            .execute()

        all_messages = [first_message] + subsequent_msgs_response.data
        return all_messages

    except Exception as e:
        app.logger.error(f"Error fetching consecutive messages: {e}")
        if hasattr(e, 'message'):
             app.logger.error(f"Supabase error details: {getattr(e, 'details', e.message)}")
        return None


# --- NEW Jinja Filter for Emojis ---
@app.template_filter('format_emojis')
def format_emojis_filter(text):
    if not text:
        return ""

    def replace_emoji(match):
        animated = match.group(0).startswith('<a:')
        emoji_name = match.group(1)
        emoji_id = match.group(2)
        extension = 'gif' if animated else 'png'
        url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{extension}?size=48" # Request a reasonable size
        # Use Markup to ensure the HTML isn't escaped later if used without |safe (though |safe is still recommended)
        return Markup(f'<img src="{url}" alt=":{emoji_name}:" class="custom-emoji">')

    # Regex to find custom Discord emojis (<:name:id> or <a:name:id>)
    emoji_pattern = r'<a?:([a-zA-Z0-9_]+):(\d+)>'
    # Apply the substitution
    formatted_text = re.sub(emoji_pattern, replace_emoji, text)
    return formatted_text


# --- Routes --- (Keep index, login, callback, logout as before)
@app.route('/')
def index():
    user_profile = None
    if 'discord_user' in session:
        user_info = session.get('discord_user')
        if user_info:
            try:
                profile_resp = supabase.table("user_profiles").select("elo_rating").eq("discord_id", user_info['id']).maybe_single().execute()
                if profile_resp.data: user_profile = {**user_info, **profile_resp.data}
                else: user_profile = user_info
            except Exception as e:
                app.logger.error(f"Error fetching user profile for nav: {e}")
                user_profile = user_info
    return render_template('index.html', user=user_profile)

@app.route('/login')
def login():
    discord = get_discord_oauth_session()
    authorization_url, state = discord.authorization_url(discord_authorization_base_url)
    session['oauth2_state'] = state # Store state to prevent CSRF
    return redirect(authorization_url)

@app.route('/callback')
def callback():
    if request.args.get('error'):
        return f"OAuth Error: {request.args.get('error')}"

    if 'oauth2_state' not in session or request.args.get('state') != session['oauth2_state']:
         flash("Invalid OAuth state. Please try logging in again.", "error")
         return redirect(url_for('index'))

    discord = get_discord_oauth_session(state=session['oauth2_state'])
    try:
        token = discord.fetch_token(
            discord_token_url,
            client_secret=discord_client_secret,
            authorization_response=request.url
        )
        session['discord_token'] = token

        user_info = fetch_discord_user(token)
        if user_info:
            session['discord_user'] = user_info
            profile = get_or_create_user_profile(user_info['id'], user_info['username'])
            if not profile:
                 flash("Could not create or retrieve your user profile in the database.", "error")
                 return redirect(url_for('logout'))
            flash(f"Successfully logged in as {user_info['username']}", "success")
        else:
            flash("Could not fetch your Discord user information.", "error")
            session.pop('discord_token', None)

    except Exception as e:
        app.logger.error(f"OAuth callback error: {e}")
        flash(f"An error occurred during Discord login: {e}", "error")

    session.pop('oauth2_state', None)
    return redirect(url_for('index'))


@app.route('/logout')
def logout():
    session.pop('discord_token', None)
    session.pop('discord_user', None)
    session.pop('current_game', None) # Clear game state on logout too
    flash("You have been logged out.", "info")
    return redirect(url_for('index'))

# --- Keep play, start_game, submit, leaderboard routes as modified in previous step ---
@app.route('/play', methods=['GET'])
def play():
    """Displays the 'Start Game' page."""
    if 'discord_user' not in session:
        flash("Please log in to play.", "warning")
        return redirect(url_for('login'))

    user_profile = None
    user_info = session.get('discord_user')
    if user_info:
        try:
            profile_resp = supabase.table("user_profiles").select("elo_rating").eq("discord_id", user_info['id']).maybe_single().execute()
            if profile_resp.data: user_profile = {**user_info, **profile_resp.data}
            else: user_profile = user_info
        except Exception as e:
            app.logger.error(f"Error fetching user profile for play page: {e}")
            user_profile = user_info

    return render_template(
        'play.html',
        messages=None,
        possible_years=POSSIBLE_YEARS,
        possible_channels=POSSIBLE_CHANNELS,
        user=user_profile
    )

@app.route('/start_game', methods=['POST'])
def start_game():
    """Fetches messages and renders the game round."""
    if 'discord_user' not in session:
        flash("Please log in to play.", "warning")
        return redirect(url_for('login'))

    messages = None
    attempts = 0
    max_attempts = 5
    while messages is None and attempts < max_attempts:
         messages = get_random_consecutive_messages(count=3)
         attempts += 1
         if messages is None and attempts < max_attempts:
              app.logger.warning(f"Attempt {attempts} failed to get messages, retrying...")
              time.sleep(0.2)
         elif messages is None and attempts == max_attempts:
              app.logger.error("Could not fetch a suitable set of messages after several attempts.")
              flash("Failed to start a new game round after several attempts. Please try again.", "error")
              return redirect(url_for('play'))

    session['current_game'] = {
        'channel': messages[0]['channel'],
        'year': isoparse(messages[0]['sent_at']).year if messages[0].get('sent_at') else None,
        'start_time': time.time(),
        'message_ids': [msg['id'] for msg in messages]
    }

    user_profile = None
    user_info = session.get('discord_user')
    if user_info:
        try:
            profile_resp = supabase.table("user_profiles").select("elo_rating").eq("discord_id", user_info['id']).maybe_single().execute()
            if profile_resp.data: user_profile = {**user_info, **profile_resp.data}
            else: user_profile = user_info
        except Exception as e:
            app.logger.error(f"Error fetching user profile for start_game render: {e}")
            user_profile = user_info

    return render_template(
        'play.html',
        messages=messages,
        possible_years=POSSIBLE_YEARS,
        possible_channels=POSSIBLE_CHANNELS,
        user=user_profile
    )


@app.route('/submit', methods=['POST'])
def submit():
    if 'discord_user' not in session or 'current_game' not in session:
        flash("Session expired or not logged in. Please start again.", "error")
        return redirect(url_for('login') if 'discord_user' not in session else url_for('play'))

    game_data = session['current_game']
    user_info = session['discord_user']
    session.pop('current_game', None)

    try:
        submitted_year = int(request.form.get('year'))
        submitted_channel = request.form.get('channel')
    except (TypeError, ValueError):
         flash("Invalid submission data.", "error")
         return redirect(url_for('play'))

    submission_time = time.time()
    time_taken = submission_time - game_data['start_time']

    correct_year = game_data.get('year')
    correct_channel = game_data.get('channel')

    if correct_year is None or correct_channel is None:
        flash("Error retrieving correct answer for the previous round. Please start a new game.", "error")
        return redirect(url_for('play'))

    year_correct = (submitted_year == correct_year)
    channel_correct = (submitted_channel == correct_channel)

    elo_change, new_elo = update_user_elo(user_info['id'], year_correct, channel_correct, time_taken)

    feedback = f"Time taken: {time_taken:.2f}s. "
    if elo_change is not None:
         feedback += f"ELO Change: {elo_change:+} (New ELO: {new_elo}). "
    else:
         feedback += "Error updating ELO. "
    feedback += f"Year: {'Correct!' if year_correct else f'Incorrect! (Was {correct_year})'}. "
    feedback += f"Channel: {'Correct!' if channel_correct else f'Incorrect! (Was {correct_channel})'}."
    flash(feedback, "info")

    return redirect(url_for('play'))

@app.route('/leaderboard')
def leaderboard():
     user_profile = None
     user_info = session.get('discord_user')
     if user_info:
         try:
             profile_resp = supabase.table("user_profiles").select("elo_rating").eq("discord_id", user_info['id']).maybe_single().execute()
             if profile_resp.data: user_profile = {**user_info, **profile_resp.data}
             else: user_profile = user_info
         except Exception as e:
             app.logger.error(f"Error fetching user profile for leaderboard: {e}")
             user_profile = user_info

     try:
          response = supabase.table("user_profiles").select("discord_username, elo_rating, games_played") \
               .order("elo_rating", desc=True).limit(100).execute()
          users = response.data if response.data else []
     except Exception as e:
          app.logger.error(f"Error fetching leaderboard data: {e}")
          users = []
          flash("Could not load leaderboard.", "error")

     return render_template('leaderboard.html', users=users, current_user=user_profile)

# --- Run ---
if __name__ == '__main__':
    app.run(debug=os.getenv("FLASK_DEBUG") == "1", host='0.0.0.0', port=5000)