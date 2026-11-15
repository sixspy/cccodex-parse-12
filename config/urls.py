from django.urls import path
from catalog import views

urlpatterns = [
    path("", views.root),
    path("login", views.login_page, name="login"),
    path("logout", views.logout_view),
    path("jobs/", views.job_list),
    path("jobs/<int:job_id>/", views.job_detail),
    path("jobs/<int:job_id>/parse/", views.job_parse),
    path("jobs/<int:job_id>/inspect/", views.job_inspect),
    path("jobs/<int:job_id>/land/", views.job_land),
    path("inspections/", views.inspection_list),
    path("policy/", views.policy_page),
]
