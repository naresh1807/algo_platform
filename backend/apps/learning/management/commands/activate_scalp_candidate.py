"""Explicit activation into the existing advisory-only inference slot."""
from pathlib import Path
import math
from django.conf import settings
from django.db import transaction
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from apps.learning.models import ModelRegistry
from apps.learning.scalp_ml_features import BASE_FEATURES


class Command(BaseCommand):
    help = "Verify and activate a scalping strategy candidate for advisory scoring only."

    def add_arguments(self, parser):
        parser.add_argument("registry_id", type=int)

    def handle(self, *args, **options):
        import joblib
        source = ModelRegistry.objects.filter(pk=options["registry_id"], model_name="scalp_strategy_candidate").first()
        if source is None:
            raise CommandError("The selected row is not a scalping strategy candidate.")
        artifact = Path(source.artifact_path).resolve()
        root = Path(getattr(settings, "ML_ARTIFACT_DIR", settings.BASE_DIR / "ml_artifacts")).resolve()
        if not artifact.is_relative_to(root) or not artifact.is_file():
            raise CommandError("Candidate artifact is missing or outside the model artifact directory.")
        features = source.metrics_json.get("feature_columns", [])
        if not features or not set(features).issubset(set(BASE_FEATURES) | {
            "method_ema_momentum_scalp", "method_rsi_extreme_scalp", "method_sar_volume_burst_scalp",
            "symbol_NIFTY", "symbol_BANKNIFTY"}):
            raise CommandError("Candidate feature schema is incompatible with scalp inference.")
        model = joblib.load(artifact)
        if getattr(model, "n_features_in_", None) != len(features):
            raise CommandError("Artifact feature count does not match its schema.")
        probabilities = model.predict_proba([[0.0] * len(features)])[0]
        if len(probabilities) != 2 or not all(math.isfinite(float(p)) and 0 <= p <= 1 for p in probabilities):
            raise CommandError("Candidate failed the probability smoke check.")
        if list(model.classes_) != [0, 1]:
            raise CommandError("Candidate class ordering is incompatible with inference.")
        metrics = {**source.metrics_json, "activation": {"scope": "advisory_only", "source_registry_id": source.pk,
                   "activated_at": timezone.now().isoformat(), "evaluation_warning": "Weak held-out ranking; zero selections at the 0.60 cutoff."}}
        with transaction.atomic():
            list(ModelRegistry.objects.select_for_update().filter(model_name="scalp_win_probability"))
            ModelRegistry.objects.filter(model_name="scalp_win_probability", active_flag=True).update(active_flag=False)
            active = ModelRegistry.objects.create(model_name="scalp_win_probability",
                model_version=timezone.now().strftime("%Y%m%d%H%M%S%f"), artifact_path=str(artifact),
                metrics_json=metrics, active_flag=True)
        self.stdout.write(self.style.SUCCESS(f"Activated advisory model {active.pk} from candidate {source.pk}. No execution settings changed."))
