from django import template

register = template.Library()

@register.filter
def get_field(form, field_name):
    """Retrieve a form field dynamically by field name."""
    return form[field_name] if field_name in form.fields else ''
