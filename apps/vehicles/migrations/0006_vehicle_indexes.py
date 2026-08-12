from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('vehicles', '0005_alter_vehicle_image_url'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='vehicle',
            index=models.Index(fields=['brand', 'model', 'year', 'variant'], name='vehicle_identity_idx'),
        ),
        migrations.AddIndex(
            model_name='vehicle',
            index=models.Index(fields=['brand', 'model'], name='vehicle_brand_model_idx'),
        ),
        migrations.AddIndex(
            model_name='vehicle',
            index=models.Index(fields=['fcai_category'], name='vehicle_fcai_category_idx'),
        ),
        migrations.AddIndex(
            model_name='vehicle',
            index=models.Index(fields=['powertrain_type'], name='vehicle_powertrain_idx'),
        ),
    ]
