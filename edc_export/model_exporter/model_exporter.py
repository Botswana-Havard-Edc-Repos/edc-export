import csv
import os
import string
import uuid

from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.db.models.constants import LOOKUP_SEP
from django_crypto_fields.fields import BaseField as BaseEncryptedField
from edc_base.utils import get_utcnow

from .export_history_updater import ExportHistoryUpdater
from .transaction_history_updater import TransactionHistoryUpdater
from edc_export.constants import EXPORTED

app_config = django_apps.get_app_config('edc_export')


class ModelExporterError(Exception):
    pass


class ModelExporterInvalidLookup(Exception):
    pass


class ModelExporterUnknownField(ValidationError):
    pass


class AdditionalValues:
    def __init__(self, export_datetime=None):
        self.export_uuid = str(uuid.uuid4())
        self.export_datetime = export_datetime
        self.timestamp = self.export_datetime.strftime('%Y%m%d%H%M%S')
        self.export_change_type = None


class ModelExporter(object):

    delimiter = '|'
    m2m_delimiter = ';'
    export_history_updater_cls = ExportHistoryUpdater
    transaction_history_model = 'edc_export.exportedtransaction'
    additional_values_cls = AdditionalValues
    export_folder = app_config.export_folder
    encrypted_label = '<encrypted>'

    export_fields = [
        'export_uuid', 'timestamp', 'export_datetime', 'export_change_type']
    required_fields = ['subject_identifier', 'report_datetime']
    audit_fields = [
        'hostname_created', 'hostname_modified', 'created',
        'modified', 'user_created', 'user_modified', 'revision']

    def __init__(self, queryset=None, model=None, field_names=None,
                 exclude_field_names=None, lookups=None,
                 exclude_m2m=None, encrypt=None, strip=None,
                 notification_plan_name=None):
        self._model = model
        self._model_cls = None
        self.encrypt = True if encrypt is None else encrypt
        self.exclude_m2m = exclude_m2m
        self.lookups = lookups or {}
        self.notification_plan_name = notification_plan_name
        self.queryset = queryset
        self.row = None
        self.row_instance = None

        if field_names:
            self.field_names = field_names
        else:
            self.field_names = [f.name for f in self.model_cls._meta.fields]
            if not exclude_m2m:
                for m2m in self.model_cls._meta.many_to_many:
                    self.field_names.append(m2m.name)
            if exclude_field_names:
                for f in self.field_names:
                    if f in exclude_field_names:
                        self.field_names.pop(self.field_names.index(f))

        for f in self.field_names:
            if f in self.export_fields or f in self.audit_fields or f in self.required_fields:
                self.field_names.pop(self.field_names.index(f))
        self.field_names = (self.export_fields
                            + self.required_fields
                            + self.field_names
                            + self.audit_fields)

    def model(self):
        return self.model_cls._meta.label_lower

    @property
    def model_cls(self):
        if not self._model_cls:
            try:
                self.queryset.count()
            except AttributeError:
                self._model_cls = django_apps.get_model(self._model)
            else:
                self._model_cls = self.queryset.model
        return self._model_cls

    @property
    def transaction_history_model_cls(self):
        return django_apps.get_model(self.transaction_history_model)

    def export(self, queryset=None):
        """Writes the export file and returns the file name.
        """
        self.queryset = queryset or self.queryset
        export_datetime = get_utcnow()
        formatted_model = self.model_cls._meta.label_lower.replace(".", "_")
        formatted_date = export_datetime.strftime('%Y%m%d%H%M%S')
        filename = f'{formatted_model}_{formatted_date}.csv'
        path = os.path.join(self.export_folder, filename)
        with open(path, 'w') as f:
            csv_writer = csv.DictWriter(
                f, fieldnames=self.field_names, delimiter=self.delimiter)
            csv_writer.writeheader()
            for model_obj in self.queryset:
                row = self.prepare_row(model_obj, export_datetime)
                row['timestamp'] = formatted_date
                tx_obj = self.transaction_history_model_cls.objects.get(
                    export_uuid=model_obj.export_uuid)
                row['export_change_type'] = tx_obj.export_change_type
                row['export_uuid'] = tx_obj.export_uuid
                csv_writer.writerow(row)
                tx_obj.status = EXPORTED
                tx_obj.exported_datetime = export_datetime
                tx_obj.timestamp = formatted_date
                tx_obj.save()
        export_history_updater = self.export_history_updater_cls(
            path=path,
            delimiter=self.delimiter,
            model=self.model_cls._meta.label_lower,
            filename=filename,
            notification_plan_name=self.notification_plan_name)
        export_history_updater.update()
        return path

    def prepare_row(self, model_obj=None, export_datetime=None):
        """Returns one row for the CSV writer.
        """
        additional_values = self.additional_values_cls(
            export_datetime=export_datetime)
        row = {}
        value = None
        for field_name in self.field_names:
            try:
                value = self.get_field_value(model_obj, field_name)
            except ModelExporterUnknownField as e:
                try:
                    value = getattr(model_obj, e.code)
                except AttributeError:

                    value = getattr(additional_values, e.code)
            if value is None:
                value = ''
            value = self.strip_value(value)
            row.update({field_name: value})
        return row

    def get_field_value(self, model_obj=None, field_name=None):
        """Returns a field value.
        """
        value = ''
        for f in model_obj.__class__._meta.fields:
            if f.name == field_name and issubclass(f.__class__, BaseEncryptedField) and self.encrypt:
                value = self.encrypted_label
        if value != self.encrypted_label:
            try:
                value = self.getattr(model_obj, field_name)
            except AttributeError:
                if field_name in self.lookups:
                    value = self.get_lookup_value(
                        model_obj=model_obj,
                        field_name=field_name)
                elif field_name in self.m2m_field_names:
                    value = self.get_m2m_value(model_obj, field_name)
                else:
                    raise ModelExporterUnknownField(
                        f'Unknown field name. Got {field_name}.', code=field_name)
        return value

    def get_lookup_value(self, model_obj=None, field_name=None):
        """Returns the field value by following the lookup string
        to a related instance.
        """
        value = model_obj
        lookup_string = self.lookups.get(field_name)
        for attr in lookup_string.split(LOOKUP_SEP):
            try:
                value = getattr(value, attr)
            except AttributeError:
                raise ModelExporterInvalidLookup(
                    f'Invalid lookup string. Got {lookup_string}')
        return value

    @property
    def m2m_field_names(self):
        """Returns the list of m2m field names for this model.
        """
        return [m2m.name for m2m in self.model_cls._meta.many_to_many]

    def get_m2m_value(self, model_obj=None, field_name=None):
        """Returns an m2m field value as a delimited string.
        """
        return self.m2m_delimiter.join(
            [value.name for value in getattr(model_obj, field_name).all()])

    def strip_value(self, value):
        """Returns a string cleaned of \n\t\r and double spaces.
        """
        try:
            value = value.replace(string.whitespace, ' ')
        except (TypeError, AttributeError):
            pass
        else:
            value = ' '.join(value.split())
        return value