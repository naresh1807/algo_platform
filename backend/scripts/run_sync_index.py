import os
import pprint

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()

from apps.investing.tasks import sync_index_constituents_and_prices

pprint.pprint(sync_index_constituents_and_prices.run())
