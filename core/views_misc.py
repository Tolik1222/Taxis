from django.http import HttpResponse
from django.shortcuts import redirect


def health_check(request):
    return HttpResponse("ok", content_type="text/plain")


def root_redirect(request):
    return redirect("index")
