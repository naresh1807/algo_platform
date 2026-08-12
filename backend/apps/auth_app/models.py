# Deliberately no custom models here yet. Django's built-in
# django.contrib.auth.models.User is enough for a single-operator
# personal platform (manual section 1: "personal use"). If this ever
# needs multiple users/roles, add a Profile model here with a
# OneToOneField to User rather than swapping AUTH_USER_MODEL mid-project.
