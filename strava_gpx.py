# https://github.com/marcin-gryszkalis/Strava-GPX-exporter
import requests
import json
import os
import re
import sys
import time
import itertools

gpxdir = "./gpx"
pagesize = 100

# already downloaded files
gpxes = {}

# https://developers.strava.com/docs/rate-limits/
limit_per_15_minutes = 90
limit_per_day = 900

calls_in_15m = 0
calls_in_day = 0

prevts = 0
def check_limits():
    global prevts
    global calls_in_15m
    global calls_in_day

    nowts = time.time()
    now = time.gmtime(nowts) # strava uses UTC for limits
    prevtime = time.gmtime(prevts)

    if prevtime.tm_mday != now.tm_mday: # new day, reset limits
        calls_in_15m = 0
        calls_in_day = 0
        prevts = nowts
        return

    if prevtime.tm_min // 15 != now.tm_min // 15: # new period, reset 15m limit
        calls_in_15m = 0

    if (calls_in_day >= limit_per_day): # limit reached, let's sleep
        d = 86400
        waittill = (nowts // d + 1) * d
        needtosleep = int(waittill - nowts) + 1
        print(f"Strava daily limit reached, sleeping {needtosleep} seconds")
        time.sleep(needtosleep)
    elif (calls_in_15m >= limit_per_15_minutes): # limit reached, let's sleep
        d = 15 * 60
        waittill = (nowts // d + 1) * d
        needtosleep = int(waittill - nowts) + 1
        print(f"Strava 15 minutes limit reached, sleeping {needtosleep} seconds")
        time.sleep(needtosleep)
    else:
        prevts = nowts

    calls_in_15m += 1
    calls_in_day += 1


def die(msg, r):
    fault = json.loads(r.text)
    fm = fault["message"]
    print(f"[status:{r.status_code}] {msg}: {fm}")
    exit(1)


def get_credential():
    if not (os.path.isfile("CLIENT_ID") and os.path.isfile("CLIENT_SECRET")):
        get_credential_from_user()
    client_id = open("CLIENT_ID", "r").read()
    client_secret = open("CLIENT_SECRET", "r").read()
    return client_id, client_secret


def get_credential_from_user():
    print("Visit https://www.strava.com/settings/api")
    print("Your Client ID: ", end="")
    client_id = input().strip()
    with open("CLIENT_ID", "w") as f:
        f.write(client_id)
    print("Client Secret: ", end="")
    client_secret = input().strip()
    with open("CLIENT_SECRET", "w") as f:
        f.write(client_secret)


def get_access_token(client_id, client_secret):
    if os.path.isfile("token.json"):
        token_json = json.load(open("token.json"))
    else:
        code = get_access_code(client_id)
        url = f"https://www.strava.com/oauth/token?client_id={client_id}&client_secret={client_secret}&code={code}&grant_type=authorization_code"
        r = requests.post(url)
        if r.status_code != 200:
            die(f"Error occurred when getting an access code", r)
        check_limits()
        api_response = json.loads(r.text)
        with open("token.json", "w") as f:
            json.dump(api_response, f)


def get_access_code(client_id):
    url = f"https://www.strava.com/oauth/authorize?client_id={client_id}&redirect_uri=http://localhost&response_type=code&scope=activity:read_all"
    print("Copy the following URL and paste in your browser's URL space, then 'Authorize':")
    print(url)
    print("Once you see an 'error page', copy the URL in the browser and paste here: ", end="")
    raw_url = input().strip()
    pat = re.compile('&code=([\da-z]+)&')
    code = pat.findall(raw_url)[0]
    return code


def get_short_lived_token(client_id, client_secret):
    api_response = json.load(open("token.json"))
    if time.time() >= api_response["expires_at"]:
        get_long_lived_token(client_id, client_secret, api_response["refresh_token"])
        api_response = json.load(open("token.json"))
    payload = {'Authorization': f'Bearer {api_response["access_token"]}'}
    return payload


def get_long_lived_token(client_id, client_secret, refresh_token):
    url = f"https://www.strava.com/oauth/token?client_id={client_id}&client_secret={client_secret}&refresh_token={refresh_token}&grant_type=refresh_token"
    r = requests.post(url)
    if r.status_code != 200:
        die(f"Error occurred when getting a long-lived token", r)
    check_limits()
    api_response = json.loads(r.text)
    with open("token.json", "w") as f:
        json.dump(api_response, f)
    print("Successfully retrived a new access token")


def list_activities(payload, page):
    url = f"https://www.strava.com/api/v3/athlete/activities"
    param = {'per_page': pagesize, 'page': page}
    r = requests.get(url, headers=payload, params=param)
    if r.status_code != 200:
        die(f"Error occurred when getting activity list", r)
    check_limits()
    return r.json()


def save_activity(payload, activity):
    activity_id = activity["id"]
    activity_name = re.sub('[^a-zA-Z0-9_-]', '_', activity["name"])
    activity_start_date = re.sub('Z$', '+0000', activity['start_date'])
    activity_start_time = time.mktime(time.strptime(activity_start_date, '%Y-%m-%dT%H:%M:%S%z')) + activity["utc_offset"]
    stime = time.strftime('%Y-%m-%d', time.gmtime(activity_start_time))

    fn = f'{stime}_{activity_id}_{activity_name}_-_{activity["sport_type"]}.gpx'
    if fn in gpxes:
        return

    output_filename = os.path.join(gpxdir, fn)

    stream_data = get_activity_stream(payload, activity_id)
    if stream_data:
        stream2gpx(stream_data, output_filename, activity_name, activity_start_time)


def get_activity_stream(payload, activity_id):
    url = f"https://www.strava.com/api/v3/activities/{activity_id}/streams"
    # for "moving" (pause logic) you may check https://github.com/cpfair/tapiriik/blob/master/tapiriik/services/Strava/strava.py
    keys = ["latlng", "altitude", "time", "heartrate", "cadence", "temp", "watts", "moving"]
    param = {"keys": ",".join(keys)}

    r = requests.get(url, headers=payload, params=param)
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        die(f"Error occurred when getting activity stream ({activity_id})", r)
    check_limits()

    j = json.loads(r.text)

    res = {}
    for k in keys:
        res[k] = []

    for s in j:
        res[s["type"]] = s["data"]

    return res;


def tpxf(name, value):
    if not value:
        return ""
    return f"<gpxtpx:{name}>{value}</gpxtpx:{name}>"


def stream2gpx(stream_data, output_filename, activity_name, activity_start_time):
    if len(stream_data["latlng"]) < 2:
        return -1
    with open(output_filename, "w") as f:
        f.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<gpx creator="Strava-GPX-Exporter" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://www.topografix.com/GPX/1/1 http://www.topografix.com/GPX/1/1/gpx.xsd http://www.garmin.com/xmlschemas/GpxExtensions/v3 http://www.garmin.com/xmlschemas/GpxExtensionsv3.xsd http://www.garmin.com/xmlschemas/TrackPointExtension/v1 http://www.garmin.com/xmlschemas/TrackPointExtensionv1.xsd" version="1.1" xmlns="http://www.topografix.com/GPX/1/1" xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1" xmlns:gpxx="http://www.garmin.com/xmlschemas/GpxExtensions/v3">
<metadata>
    <name>{activity_name}</name>
</metadata>
<trk>
    <name>{activity_name}</name>
    <trkseg>
""")

        for (lat, lon), timeoffset, alt, hr, cad, temp, power in itertools.zip_longest(stream_data["latlng"], stream_data["time"], stream_data["altitude"], stream_data["heartrate"], stream_data["cadence"], stream_data["temp"], stream_data["watts"]):
            rtime = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(activity_start_time + timeoffset))
            f.write(f"""
<trkpt lat="{lat}" lon="{lon}">
    <time>{rtime}</time>
    <ele>{alt}</ele>
    <extensions>
      <gpxtpx:TrackPointExtension>
          {tpxf('hr',hr)}
          {tpxf('cad',cad)}
          {tpxf('temp',temp)}
          {tpxf('power',power)}
      </gpxtpx:TrackPointExtension>
    </extensions>
</trkpt>
""")
        f.write("    </trkseg>\n</trk>\n</gpx>")


def main():

    client_id, client_secret = get_credential()
    get_access_token(client_id, client_secret)
    payload = get_short_lived_token(client_id, client_secret)

    # get existing files
    os.makedirs(gpxdir, exist_ok=True)
    for f in os.listdir(gpxdir):
        if os.path.isfile(os.path.join(gpxdir, f)):
            gpxes[f] = True

    i = 1
    page = 1
    while True:
        activities = list_activities(payload, page)
        if not activities:
            break

        for a in activities:
            manualmsg = ""
            if a["manual"]:
                manualmsg = " MANUAL"
            print(f'{i : >5}. {a["id"]} {a["sport_type"]}: {a["name"]}{manualmsg}')

            if a["manual"]:
                continue

            save_activity(payload, a)
            i += 1
#            exit()

        page += 1
        

if __name__ == "__main__":
    main()
