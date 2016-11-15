from django.db import models

from edc_base.model.models import BaseUuidModel, HistoricalRecords


class ExportPlanManager(models.Manager):

    def get_by_natural_key(self, app_label, model_name):
        return self.get(app_label=app_label, model_name=model_name)


class ExportPlan(BaseUuidModel):

    app_label = models.CharField(max_length=50)

    model_name = models.CharField(max_length=50)

    fields = models.TextField(max_length=500)

    extra_fields = models.TextField(max_length=500)

    exclude = models.TextField(max_length=500)

    header = models.BooleanField(default=True)

    track_history = models.BooleanField(default=True)

    show_all_fields = models.BooleanField(default=True)

    delimiter = models.CharField(max_length=1, default=',')

    encrypt = models.BooleanField(default=False)

    strip = models.BooleanField(default=True)

    target_path = models.CharField(max_length=250, default='~/export')

    notification_plan_name = models.CharField(max_length=50, null=True)

    objects = ExportPlanManager()

    history = HistoricalRecords()

    def __str__(self):
        return '{}.{}'.format(self.app_label, self.model_name)

    def natural_key(self):
        return (self.app_label, self.model_name, )

    class Meta:
        app_label = 'edc_export'
        unique_together = (('app_label', 'model_name'), )
