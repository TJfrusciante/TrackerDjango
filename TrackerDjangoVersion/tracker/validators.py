import re

from django.core.exceptions import ValidationError


class SpecialCharacterValidator:
    def validate(self, password, user=None):
        if not re.search(r'[^A-Za-z0-9]', password or ''):
            raise ValidationError(
                'A senha deve conter ao menos um caractere especial.',
                code='password_no_special',
            )

    def get_help_text(self):
        return 'Sua senha deve conter ao menos um caractere especial.'
