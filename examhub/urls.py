"""
URL configuration for examhub project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path

from digitalexams.views import public_media

urlpatterns = [
    path('admin/', admin.site.urls),
    # Uploads are NOT served from /media/ in general: exam papers, images, videos and resources
    # only go out through the checked views (api/items/<id>/pdf/, api/resources/<id>/file/, …),
    # which know who is signed in, which school they're in and whether the exam is open. Only
    # school logos are public here, because the sign-in page shows them before anyone signs in.
    path('media/<path:path>', public_media, name='public-media'),
    path('', include('digitalexams.urls')),
]
