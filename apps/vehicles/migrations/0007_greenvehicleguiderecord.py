# GVG current-vehicle lookup (Decision Note - Vehicle Internet Lookup, 8-Aug-2026, Option 1).
# TrigramExtension requires PostgreSQL (CI + prod are postgres:16 / RDS - pg_trgm is available).
import django.contrib.postgres.indexes
from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('vehicles', '0006_vehicle_indexes'),
    ]

    operations = [
        TrigramExtension(),
        migrations.CreateModel(
            name='GreenVehicleGuideRecord',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('brand', models.CharField(max_length=255)),
                ('model', models.CharField(max_length=255)),
                ('variant', models.CharField(blank=True, default='', max_length=255)),
                ('year', models.IntegerField()),
                ('body_style', models.CharField(blank=True, default='', max_length=100)),
                ('engine_desc', models.CharField(blank=True, default='', max_length=100)),
                ('fuel_type_raw', models.CharField(blank=True, default='', max_length=100)),
                ('powertrain_type', models.CharField(blank=True, choices=[('petrol', 'Petrol'), ('diesel', 'Diesel'), ('mild_hybrid', 'Mild Hybrid'), ('hybrid', 'Hybrid'), ('phev', 'Plug-in Hybrid (PHEV)'), ('bev', 'Battery Electric (BEV)'), ('fcev', 'Hydrogen / FCEV')], default='', max_length=50)),
                ('fuel_consumption_per_100km', models.FloatField(blank=True, null=True)),
                ('electric_energy_wh_per_km', models.FloatField(blank=True, null=True)),
                ('tailpipe_co2_g_per_km', models.FloatField(blank=True, null=True)),
                ('electric_range_km', models.FloatField(blank=True, null=True)),
                ('test_cycle', models.CharField(blank=True, choices=[('WLTP', 'WLTP'), ('ADR', 'ADR'), ('NEDC', 'NEDC')], default='', max_length=20)),
                ('source_dataset', models.CharField(choices=[('S-GVG-CURRENT', 'GVG current-vehicles CSV'), ('S-GVG-OLDER-8603', 'GVG older-vehicles CSV (1986-2003 Fuel Consumption Guide)')], max_length=30)),
                ('source_url', models.URLField(blank=True, default='', max_length=500)),
                ('retrieved_at', models.DateTimeField(blank=True, null=True)),
                ('raw_row', models.JSONField(blank=True, default=dict)),
                ('search_text', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['brand', 'model', 'year', 'variant'],
            },
        ),
        migrations.AddConstraint(
            model_name='greenvehicleguiderecord',
            constraint=models.UniqueConstraint(fields=('brand', 'model', 'variant', 'year'), name='gvg_identity_uniq'),
        ),
        migrations.AddIndex(
            model_name='greenvehicleguiderecord',
            index=models.Index(fields=['brand', 'model'], name='gvg_brand_model_idx'),
        ),
        migrations.AddIndex(
            model_name='greenvehicleguiderecord',
            index=models.Index(fields=['year'], name='gvg_year_idx'),
        ),
        migrations.AddIndex(
            model_name='greenvehicleguiderecord',
            index=django.contrib.postgres.indexes.GinIndex(fields=['search_text'], name='gvg_search_trgm_idx', opclasses=['gin_trgm_ops']),
        ),
    ]
