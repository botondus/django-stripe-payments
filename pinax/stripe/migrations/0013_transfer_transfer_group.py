# -*- coding: utf-8 -*-
# Generated by Django 1.10.2 on 2017-03-02 20:59
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pinax_stripe', '0012_auto_20170216_1522'),
    ]

    operations = [
        migrations.AddField(
            model_name='transfer',
            name='transfer_group',
            field=models.TextField(blank=True, null=True),
        ),
    ]
