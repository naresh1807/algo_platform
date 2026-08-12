from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import HistoricalDataViewSet, IndicatorSeriesView

router = DefaultRouter()
router.register("candles", HistoricalDataViewSet, basename="historical-data")

urlpatterns = router.urls + [
    path("indicators/", IndicatorSeriesView.as_view(), name="indicator-series"),
]
