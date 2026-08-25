from django.db import models
#dashboard/models.py
from django.db import models
    
class ChamaInfo(models.Model):
    brand_name = models.CharField(max_length=255, default="NODi Lite")
    brand_footer = models.CharField(max_length=255, default="NODi Core Banking System ver.1.0")
    chama_name = models.CharField(max_length=255)
    chama_address = models.TextField()
    chama_contact = models.CharField(max_length=50)
    chama_location = models.CharField(max_length=255)
    chama_footer = models.CharField(max_length=255,  blank=True, null=True,default="NODi Core Banking System ver.2.0")
    chama_logo = models.ImageField(upload_to='chama_logos/', blank=True, null=True)
    created_at=  models.DateTimeField(auto_now_add=True)
    
    class Meta:
        verbose_name = "Chama Information"
        verbose_name_plural = "Chama Information"

    def __str__(self):
        return self.chama_name
from django.db import models

class CompanyBranch(models.Model):
    name = models.CharField(max_length=100, unique=True)
    branch_code = models.CharField(max_length=20, unique=True, blank=True) # Blank allowed since it autogenerates
    is_active = models.BooleanField(default=True)
    is_headquarters = models.BooleanField(default=False)
    email = models.EmailField(max_length=254, null=True, blank=True)
    phone_number = models.CharField(max_length=20, null=True, blank=True)
    street_address = models.TextField()
    city = models.CharField(max_length=100)
    state_province = models.CharField(max_length=100, null=True, blank=True)
    postal_code = models.CharField(max_length=20, null=True, blank=True)
    country = models.CharField(max_length=100, default="Kenya")
    timezone = models.CharField(max_length=50, default="EAT")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name = "Company Branch"
        verbose_name_plural = "Company Branches"

    def __str__(self):
        hq_label = " (HQ)" if self.is_headquarters else ""
        return f"{self.name} - {self.branch_code}{hq_label}"

    def save(self, *args, **kwargs):
        # 1. Enforce single HQ rule
        if self.is_headquarters:
            CompanyBranch.objects.filter(is_headquarters=True).exclude(pk=self.pk).update(is_headquarters=False)
        
        # 2. First save to guarantee an ID exists if it's a new instance
        is_new = self.pk is None
        super(CompanyBranch, self).save(*args, **kwargs)
        
        # 3. Generate BXXX code format using the newly acquired ID
        if is_new and not self.branch_code:
            self.branch_code = f"B{self.id:03d}"  # Pads with zeros (e.g., ID 5 becomes B005)
            super(CompanyBranch, self).save(update_fields=['branch_code'])
            
class BackupLog(models.Model):
    STATUS_CHOICES = [('success', 'Success'), ('error', 'Error')]
    
    timestamp = models.DateTimeField(auto_now_add=True)
    file_name = models.CharField(max_length=255)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES)
    message = models.TextField(blank=True)  # To store error logs if it fails
    file_size = models.CharField(max_length=50, blank=True)

    class Meta:
        ordering = ['-timestamp']

class BackupConfiguration(models.Model):
    interval_hours = models.PositiveIntegerField(default=24)
    # --- NEW FIELD ---
    email_recipient = models.EmailField(
        help_text="The email address that will receive the .sql backup file."
    )
    is_active = models.BooleanField(default=True)
    last_run = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Backup config for {self.email_recipient}"


# ════════════════════════════════════════════════════════════════════════════════
# PROMOTION — ads / updates shown in the mobile app dashboard carousel
# ════════════════════════════════════════════════════════════════════════════════

class Promotion(models.Model):
    """
    Promotion / Ad / Announcement displayed in the customer mobile app carousel.
    """
    PROMO_TYPE_CHOICES = [
        ('ad',           'Advertisement'),
        ('update',       'Service Update'),
        ('announcement', 'Announcement'),
        ('product',      'New Product'),
    ]

    title = models.CharField(max_length=120, help_text="Short headline shown on the card.")
    subtitle = models.CharField(max_length=255, blank=True, help_text="Optional second line.")
    body = models.TextField(blank=True, help_text="Full message shown when tapped.")
    image = models.ImageField(
        upload_to='promotions/', blank=True, null=True,
        help_text="Banner image, recommended 16:9 aspect (e.g. 1280x720).",
    )
    promo_type = models.CharField(max_length=20, choices=PROMO_TYPE_CHOICES, default='ad')
    cta_label = models.CharField(max_length=40, blank=True, help_text="Optional call-to-action label.")
    cta_url = models.URLField(blank=True, help_text="Optional link the CTA opens.")
    is_active = models.BooleanField(default=True)
    display_order = models.PositiveIntegerField(default=0, help_text="Lower = shown earlier.")
    start_date = models.DateTimeField(null=True, blank=True, help_text="Optional schedule start.")
    end_date = models.DateTimeField(null=True, blank=True, help_text="Optional schedule end.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['display_order', '-created_at']
        verbose_name = "Promotion"
        verbose_name_plural = "Promotions"

    def __str__(self):
        return f"[{self.get_promo_type_display()}] {self.title}"

    def is_live(self):
        """True if the promotion is active AND within its scheduled window."""
        from django.utils import timezone
        if not self.is_active:
            return False
        now = timezone.now()
        if self.start_date and now < self.start_date:
            return False
        if self.end_date and now > self.end_date:
            return False
        return True