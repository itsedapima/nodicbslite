from django.urls import path

from . import views

app_name = "data_imports"

urlpatterns = [
    path("", views.hub, name="hub"),
    path("<slug:slug>/template/", views.download_template, name="download_template"),
    path("<slug:slug>/upload/", views.upload, name="upload"),
    path("batch/<str:batch_id>/preview/", views.preview, name="preview"),
    path("batch/<str:batch_id>/commit/", views.commit, name="commit"),
    path("batch/<str:batch_id>/summary/", views.summary, name="summary"),
    path("batch/<str:batch_id>/summary.pdf", views.summary_pdf, name="summary_pdf"),
]
