"""Location privacy policy for the production app.

The user's live GPS position must remain on the device. Backend route and weather
calculations use a fixed Gyeongju service anchor instead of user coordinates.
Public coordinates of tourist places may still be stored and returned.
"""

GYEONGJU_CENTER_LATITUDE = 35.8562
GYEONGJU_CENTER_LONGITUDE = 129.2247
