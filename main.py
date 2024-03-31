from pymongo import MongoClient
from dotenv import load_dotenv
import certifi
import os
from datetime import datetime, timedelta
import aiohttp
import asyncio
import time 

# Load environment variables from .env file
load_dotenv()
ca = certifi.where()
client = MongoClient(os.getenv("MONGO_DB_URI"), tlsCAFile=ca)
db = client["league_discord_bot"]


class AsyncRateLimiter:
    def __init__(self, max_calls, period):
        self.max_calls = max_calls
        self.period = period
        self.calls = []
    
    async def wait(self):
        now = datetime.now()
        
        # Remove calls that are outside of the current period
        self.calls = [call for call in self.calls if now - call < timedelta(seconds=self.period)]
        
        if len(self.calls) >= self.max_calls:
            # Calculate the time to sleep
            oldest_call = min(self.calls)
            sleep_time = (oldest_call + timedelta(seconds=self.period)) - now
            print(f"Rate limit almost reached. Sleeping for {sleep_time.total_seconds()} seconds")
            await asyncio.sleep(sleep_time.total_seconds())
        
        # Record the current call
        self.calls.append(datetime.now())


# Handler function for all calls made to riot api
async def handle_api_call(url):
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as response:
                if response.status == 429:  # Rate limit exceeded
                    retry_after = int(response.headers.get("Retry-After", 1))
                    print(f"Rate limit exceeded. Retrying in {retry_after} seconds.")
                    await asyncio.sleep(retry_after)
                    return await handle_api_call(url)  # Retry the request
                response.raise_for_status()  # Raise an exception for non-200 status codes
                data = await response.json()
                return data
        except aiohttp.ClientResponseError as e:
            print(f"Error in API call: {e.status}, message='{e.message}'")
            return None

# Retrieves a list of all summoners for given discord server
async def get_summoners(guild_id):
    # Retrieve the document for the server
    collection = db.discord_servers
    document = collection.find_one({"guild_id": guild_id})

    if document and "summoners" in document:
        summoners_list = document["summoners"]
        return summoners_list

    return None


# Retrieves a list of all guilds
async def get_guilds():
    collection = db.discord_servers
    documents = collection.find()

    if not documents:
        print("No documents found in discord_servers.")
    else:
        return list(documents)


async def run_at_start_of_next_hour():
    while True:
        # Calculate seconds until the next hour
        now = datetime.now()
        next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        wait_seconds = (next_hour - now).total_seconds()

        # Wait until the start of the next hour
        print(f"Waiting {next_hour - now} until the next hour.")
        await asyncio.sleep(wait_seconds)

        # Run the main function
        guilds = await get_guilds()
        await cache_match_data(guilds)

        # Now wait for one hour before the loop runs the main function again
        await asyncio.sleep(10)


async def cache_match_data(guilds):
    rate_limiter = AsyncRateLimiter(100, 120)
    collection = db.cached_match_data
    summoners_checked = []
    num_total_matches_cached = 0

    job_start_time = datetime.now()
    formatted_job_start_time = job_start_time.strftime("%m/%d/%y %H:%M:%S")

    print(f"Caching all data from the last 30 days (Started at {formatted_job_start_time})...")

    for guild in guilds:
        summoners = await get_summoners(guild["guild_id"])  # Assume this is implemented
        for summoner in summoners:
            if summoner not in summoners_checked:
                matches_cached = 0
                days_fetched = 0
                summoner_puuid = summoner["puuid"]
                while days_fetched < 30:
                    days_to_fetch = min(5, 30 - days_fetched)
                    end_time = datetime.today() - timedelta(days=days_fetched)
                    start_time = end_time - timedelta(days=days_to_fetch)
                    end_timestamp = int(end_time.timestamp())
                    start_timestamp = int(start_time.timestamp())
                    
                    print(f"Fetching matches from {start_time.strftime('%Y-%m-%d')} to {end_time.strftime('%Y-%m-%d')} for summoner {summoner["name"]}.")

                    url = f"https://americas.api.riotgames.com/lol/match/v5/matches/by-puuid/{summoner_puuid}/ids?startTime={start_timestamp}&endTime={end_timestamp}&queue=420&start=0&count=100&api_key={os.getenv('RIOT_API_KEY')}"
                    await rate_limiter.wait()
                    match_ids = await handle_api_call(url)

                    # Find documents where `metadata.matchId` is in list of match IDs
                    matched_documents = collection.find({"metadata.matchId": {"$in": match_ids}})

                    # Extract the matched IDs
                    matched_ids = [doc['metadata']['matchId'] for doc in matched_documents]

                    # Filter your list to remove the matched IDs
                    unmatched_ids = [mid for mid in match_ids if mid not in matched_ids]

                    print(f"Matches already cached: {matched_ids}")
                    print(f"Matches being cached: {unmatched_ids}")

                    if unmatched_ids:
                        for match_id in unmatched_ids:
                            match_url = f"https://americas.api.riotgames.com/lol/match/v5/matches/{match_id}?api_key={os.getenv('RIOT_API_KEY')}"
                            await rate_limiter.wait()
                            single_match_data = await handle_api_call(match_url)

                            if single_match_data:
                                matches_cached += 1
                                num_total_matches_cached += 1
                                collection.insert_one(single_match_data)

                    days_fetched += days_to_fetch
                
                summoners_checked.append(summoner)
                print(f"{matches_cached} matches staged to be cached.")
            else:
                print(f"Already iterated through summoner {summoner["name"]}")

    print(f"{num_total_matches_cached} matches cached into cached_matches_data collection.")
    job_end_time = datetime.now()
    elapsed_time = job_end_time - job_start_time
    elapsed_time_seconds = int(elapsed_time.total_seconds())
    hours = elapsed_time_seconds // 3600
    minutes = (elapsed_time_seconds % 3600) // 60
    seconds = elapsed_time_seconds % 60
    formatted_elapsed_time = f"{hours:02}:{minutes:02}:{seconds:02}"
    print(f"\nDone caching all match data from the last 30 days. Took {formatted_elapsed_time}")

if __name__ == "__main__":
    asyncio.run(run_at_start_of_next_hour())
