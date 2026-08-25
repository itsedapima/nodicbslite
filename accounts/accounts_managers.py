from django.contrib.auth.base_user import BaseUserManager


class CustomUserManager(BaseUserManager):

    def create_user(self, username, email, phone, first_name, last_name,
                    password=None, role="customer"):
        if not email:
            raise ValueError('Users must have an email address')
        if not username:
            raise ValueError('Users must have a username')
        if not phone:
            raise ValueError('Users must have a phone number')

        user = self.model(
            email=self.normalize_email(email),
            username=username,
            phone=phone,
            first_name=first_name,
            last_name=last_name,
            role=role,
        )
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_mobile_user(self, cust_no, email, password):
        """
        Onboard an existing SACCO member via mobile app.
        Pulls first_name, last_name, phone from the Customer record
        linked to cust_no, then creates an auth user and links them.

        Usage:
            CustomUser.objects.create_mobile_user(
                cust_no='C001', email='member@email.com', password='secret'
            )
        """
        from customers.models import Customer  # local import to avoid circular

        try:
            customer = Customer.objects.get(cust_no=cust_no)
        except Customer.DoesNotExist:
            raise ValueError(f'No customer found with customer number: {cust_no}')

        if customer.user is not None:
            raise ValueError(
                f'Customer {cust_no} already has a mobile account. '
                'Please use the login screen.'
            )

        if not email:
            raise ValueError('Email address is required')

        if self.model.objects.filter(email=self.normalize_email(email)).exists():
            raise ValueError('An account with this email already exists')

        # Build username from cust_no (unique, clean)
        username = f"cust_{cust_no.lower().replace(' ', '_')}"

        user = self.model(
            email=self.normalize_email(email),
            username=username,
            phone=getattr(customer, 'phone', ''),
            first_name=getattr(customer, 'first_name', ''),
            last_name=getattr(customer, 'last_name', ''),
            role='customer',
            is_mobile_verified=True,
        )
        user.set_password(password)
        user.save(using=self._db)

        # Link the customer record to this user
        customer.user = user
        customer.save(update_fields=['user'])

        return user

    def create_superuser(self, username, email, phone, first_name, last_name,
                         password=None):
        user = self.create_user(
            username=username,
            email=email,
            phone=phone,
            first_name=first_name,
            last_name=last_name,
            password=password,
            role='admin',
        )
        user.is_staff = True
        user.is_superuser = True
        user.save(using=self._db)
        return user
