from django.contrib.auth.models import User
from django.core.files.storage import FileSystemStorage
from django.db import models
from django.contrib.sites.models import Site
from django.core import urlresolvers
from PIL import Image
import os, datetime
from django.conf import settings
from django.core.files.storage import default_storage
import uuid
import stripe
from django.db.models import Q
from decimal import Decimal
from django.utils.safestring import mark_safe
import calendar
from django.utils import timezone
from django.core.urlresolvers import reverse
from gather.tasks import published_events_today_local, events_pending
from gather.forms import NewUserForm
from django.contrib.flatpages.models import FlatPage

# imports for signals
import django.dispatch
from django.dispatch import receiver
from django.db.models.signals import pre_save, post_save

# mail imports
from django.core.mail import EmailMultiAlternatives
from django.template.loader import get_template
from django.template import Context

import logging

logger = logging.getLogger(__name__)

# there is a weird db issue it seems with setting a field to null=False after it has been defined as null=True.
# see http://od-eon.com/blogs/stefan/adding-not-null-column-south/ and
# http://south.aeracode.org/ticket/782
# one suggestion was to try setting default value in the model file, but this hasn't worked either.
# currently the field are still set to null=True, though they shouldn't be.

def location_img_upload_to(instance, filename):
	ext = filename.split('.')[-1]
	# rename file to random string
	filename = "%s.%s" % (uuid.uuid4(), ext.lower())

	upload_path = "locations/"
	upload_abs_path = os.path.join(settings.MEDIA_ROOT, upload_path)
	if not os.path.exists(upload_abs_path):
		os.makedirs(upload_abs_path)
	return os.path.join(upload_path, filename)

def default_location():
	return Location.objects.get(pk=1)

class Location(models.Model):
	name = models.CharField(max_length=200)
	slug = models.CharField(max_length=60, unique=True, help_text="Try to make this short and sweet. It will also be used to form several location-specific email addresses in the form of xxx@<your_slug>.mail.embassynetwork.com")
	short_description = models.TextField()
	address = models.CharField(max_length=300)
	latitude = models.FloatField()
	longitude = models.FloatField()
	image = models.ImageField(upload_to=location_img_upload_to, help_text="Requires an image with proportions 1400px wide x 300px high")
	stay_page = models.TextField()
	front_page_stay = models.TextField()
	front_page_participate = models.TextField()
	announcement = models.TextField(blank=True, null=True)
	max_reservation_days = models.IntegerField(default=14)
	welcome_email_days_ahead = models.IntegerField(default=2)
	house_access_code = models.CharField(max_length=50, blank=True, null=True)
	ssid = models.CharField(max_length=200, blank=True, null=True)
	ssid_password = models.CharField(max_length=200, blank=True, null=True)
	timezone = models.CharField(max_length=200, help_text="Must be an accurate timezone name, eg. \"America/Los_Angeles\"")
	bank_account_number = models.IntegerField(max_length=200, blank=True, null=True, help_text="We use this to transfer money to you!")
	routing_number = models.IntegerField(max_length=200, blank=True, null=True, help_text="We use this to transfer money to you!")
	bank_name = models.CharField(max_length=200, blank=True, null=True, help_text="We use this to transfer money to you!")
	name_on_account = models.CharField(max_length=200, blank=True, null=True, help_text="We use this to transfer money to you!")
	email_subject_prefix = models.CharField(max_length=200, help_text="Your prefix will be wrapped in square brackets automatically.")
	house_admins = models.ManyToManyField(User, related_name='house_admin', blank=True, null=True)
	residents = models.ManyToManyField(User, related_name='residences', blank=True, null=True)
	check_out = models.CharField(max_length=20, help_text="When your guests should be out of their bed/room.")
	check_in = models.CharField(max_length=200, help_text="When your guests can expect their bed to be ready.")
	public = models.BooleanField(default=False, verbose_name="Is this location open to the public?")

	def __unicode__(self):
		return self.name

	def get_absolute_url(self):
		return reverse('core.views.location', args=[str(self.slug)])

	def from_email(self):
		''' return a location-specific email in the standard format we use.'''
		return "stay@%s.mail.embassynetwork.com" % self.slug

	def get_rooms(self):
		return list(Room.objects.filter(location=self))

	def rooms_with_future_reservability(self):
		future_reservability = []
		for room in Room.objects.filter(location=self):
			if room.future_reservability():
				future_reservability.append(room)
		return future_reservability

	def _rooms_with_future_reservability_queryset(self):
		today = timezone.localtime(timezone.now())
		return Room.objects.filter(reservables__isnull=False).filter(location=self).filter(Q(reservables__end_date__gte=today) | Q(reservables__end_date=None))

	def reservable_rooms_on_day(self, the_day):
		rooms_at_location = self.filter(location=self)
		return [room for room in rooms_at_location if room.is_reservable(the_day)]

	def availability(self, start, end):
		# show availability (occupied and free beds), between start and end
		# dates, per location. create a structure queryable by
		# available_beds[room][date] = n, where n is the number of beds free. 
		rooms_at_location = self.get_rooms()
		available_beds = {}
		for room in rooms_at_location:
			the_day = start
			available_beds[room] = {}
			while the_day < end:
				if not room.is_reservable(the_day):
					available_beds[room][the_day] = 0
				else:
					available_beds[room][the_day] = room.beds
					bookings_today = Reservation.objects.confirmed_approved_on_date(the_day, self, room=room)
					for booking in bookings_today:
						available_beds[room][the_day] = available_beds[room][the_day] - 1
				the_day = the_day + datetime.timedelta(1)
		return available_beds

	def rooms_free(self, arrive, depart):
		available = list(self.rooms.all())
		for room in self.get_rooms():
			the_day = arrive
			while the_day < depart:
				if not room.available_on(the_day):
					available.remove(room)
					break
				the_day = the_day + datetime.timedelta(1)
		return available

	def has_availability(self, arrive=None, depart=None):
		if not arrive:
			arrive = timezone.localtime(timezone.now())
			depart = arrive + datetime.timedelta(1)
		if not self.rooms_free(arrive, depart):
			return False
		return True

	def events(self, user=None):
		today = timezone.localtime(timezone.now())
		if 'gather' in settings.INSTALLED_APPS:
			from gather.models import Event
			return Event.objects.upcoming(upto=5, current_user=user, location=self)
		return None

	def coming_month_events(self, days=30):
		today = timezone.localtime(timezone.now())
		if 'gather' in settings.INSTALLED_APPS:
			from gather.models import Event
			return Event.objects.filter(status="live").filter(location=self).exclude(end__lt=today).exclude(start__gte=today+datetime.timedelta(days=days))
		return None

	def coming_month_reservations(self, days=30):
		today = timezone.localtime(timezone.now())
		return Reservation.objects.filter(Q(status="confirmed") | Q(status="approved")).filter(location=self).exclude(depart__lt=today).exclude( arrive__gt=today+datetime.timedelta(days=days))
	
	def people_in_coming_month(self):
		# pull out all reservations in the coming month
		people = []
		for r in self.coming_month_reservations():
			if r.user not in people:
				people.append(r.user)

		# add residents to the list of people in the house in the coming month. 
		for r in self.residents.all():
			if r not in people:
				people.append(r)

		# add house admins
		for a in self.house_admins.all():
			if a not in people:
				people.append(a)

		# Add all the people from events too
		for e in self.coming_month_events():
			for u in e.organizers.all():
				if u not in people:
					people.append(u)

		return people
	
	def get_menus(self):
		return LocationMenu.objects.filter(location=self)


class LocationNotUniqueException(Exception):
	pass

class LocationDoesNotExistException(Exception):
	pass

def get_location(location_slug):
	if location_slug:
		try:
			location = Location.objects.get(slug=location_slug)
		except:
			raise LocationDoesNotExistException("The requested location does not exist: %s" % location_slug)
	else:
		if Location.objects.count() == 1:
			location = Location.objects.get(id=1)
		else:
			raise LocationNotUniqueException("You did not specify a location and yet there is more than one location defined. Please specify a location.")
	return location

def room_img_upload_to(instance, filename):
	ext = filename.split('.')[-1]
	# rename file to random string
	filename = "%s.%s" % (uuid.uuid4(), ext.lower())

	upload_path = "rooms/"
	upload_abs_path = os.path.join(settings.MEDIA_ROOT, upload_path)
	if not os.path.exists(upload_abs_path):
		os.makedirs(upload_abs_path)
	return os.path.join(upload_path, filename)

class RoomCalendar(calendar.HTMLCalendar):
	def __init__(self, room, location, year, month):
		super(RoomCalendar, self).__init__()
		self.year = year
		self.month = month
		self.room = room
		self.location = location
		self.today = timezone.now()
		self.setfirstweekday(calendar.SUNDAY)

	def formatday(self, day, weekday):
		# XXX warning: if there are ANY errors this method seems to just punt
		# and return None. makes it very hard to debug.
		if day == 0:
			return '<td class="noday">&nbsp;</td>' # day outside month
		else:
			if self.today.date() == datetime.date(self.year, self.month, day):
				cssclasses = self.cssclasses[weekday] + ' today'
			else:
				cssclasses = self.cssclasses[weekday]
			the_day = datetime.date(self.year, self.month, day)
			if self.room.available_on(the_day):
				return '<td class="%s"><span class="text-success glyphicon glyphicon-ok"></span> %d</td>' % (cssclasses, day)
			else:
				return '<td class="%s"><span class="text-danger glyphicon glyphicon-remove"></span> %d</td>' % (cssclasses, day)

class Room(models.Model):
	name = models.CharField(max_length=200)
	location = models.ForeignKey(Location, related_name='rooms', null=True)
	default_rate = models.IntegerField()
	description = models.TextField(blank=True, null=True)
	cancellation_policy = models.CharField(max_length=400, default="24 hours")
	shared = models.BooleanField(default=False, verbose_name="Is this room a hostel/shared accommodation?")
	beds = models.IntegerField()
	residents = models.ManyToManyField(User, blank=True, null=True, related_name="residents") # a room may have many residents and a resident may have many rooms
	image = models.ImageField(upload_to=room_img_upload_to, blank=True, null=True)

	def __unicode__(self):
		return self.name

	def future_reservability(self):
		today = timezone.localtime(timezone.now())
		reservables = self.reservables.filter(Q(end_date__gte=today) | Q(end_date=None))
		if reservables:
			return True
		else:
			return False

	def is_reservable(self, this_day):
		# should never be more than 1 reservable on a given day... 
		try:
			reservable_today = self.reservables.filter(room=self).filter(start_date__lte=this_day).get(Q(end_date__gte=this_day) | Q(end_date=None))  
		except:
			reservable_today = False
		return reservable_today 

	def available_on(self, this_day):
		# a room is available if it is reservable and if it has free beds. 
		# JKS i added the filter(room=self) - need to test this. 
		if not self.is_reservable(this_day):
			return False
		reservations_on_this_day = Reservation.objects.confirmed_approved_on_date(this_day, self.location, room=self)
		beds_left = self.beds
		for r in reservations_on_this_day:
			beds_left -= 1
		if beds_left > 0:
			return True
		else:
			return False

	def availability_calendar_html(self, month=None, year=None):
		if not (month and year):
			today = timezone.localtime(timezone.now())
			month = today.month
			year = today.year
		location = self.location
		room_cal = RoomCalendar(self, location, year, month)
		month_html = room_cal.formatmonth(year, month)
		return month_html

class ReservationManager(models.Manager):

	def on_date(self, the_day, status, location):
		# return the reservations that intersect this day, of any status
		all_on_date = super(ReservationManager, self).get_query_set().filter(location=location).filter(arrive__lte = the_day).filter(depart__gt = the_day)
		return all_on_date.filter(status=status)

	def confirmed_approved_on_date(self, the_day, location, room=None):
		# return the approved or confirmed reservations that intersect this day
		approved_reservations = self.on_date(the_day, status= "approved", location=location)
		confirmed_reservations = self.on_date(the_day, status="confirmed", location=location)
		if room:
			approved_reservations = approved_reservations.filter(room=room)
			confirmed_reservations = confirmed_reservations.filter(room=room)
		return (list(approved_reservations) + list(confirmed_reservations))

	def confirmed_on_date(self, the_day, location, room=None):
		confirmed_reservations = self.on_date(the_day, status="confirmed", location=location)
		if room:
			confirmed_reservations = confirmed_reservations.filter(room=room)
		return list(confirmed_reservations)

class Reservation(models.Model):

	class ResActionError(Exception):
		def __init__(self, value):
			self.value = value
		def __str__(self):
			return repr(self.value)

	PENDING = 'pending'
	APPROVED = 'approved'
	CONFIRMED = 'confirmed'
	HOUSE_DECLINED = 'house declined'
	USER_DECLINED = 'user declined'
	CANCELED = 'canceled'

	RESERVATION_STATUSES = (
			(PENDING, 'Pending'),
			(APPROVED, 'Approved'),
			(CONFIRMED, 'Confirmed'),
			(HOUSE_DECLINED, 'House Declined'),
			(USER_DECLINED, 'User Declined'),
			(CANCELED, 'Canceled'),
		)

	location = models.ForeignKey(Location, related_name='reservations', null=True)
	created = models.DateTimeField(auto_now_add=True)
	updated = models.DateTimeField(auto_now=True)
	status = models.CharField(max_length=200, choices=RESERVATION_STATUSES, default=PENDING, blank=True)
	user = models.ForeignKey(User, related_name='reservations')
	arrive = models.DateField(verbose_name='Arrival Date')
	depart = models.DateField(verbose_name='Departure Date')
	arrival_time = models.CharField(help_text='Optional, if known', max_length=200, blank=True, null=True)
	room = models.ForeignKey(Room, null=True)
	tags = models.CharField(max_length =200, help_text='What are 2 or 3 tags that characterize this trip?', blank=True, null=True)
	purpose = models.TextField(verbose_name='Tell us a bit about the reason for your trip/stay')
	comments = models.TextField(blank=True, null=True, verbose_name='Any additional comments. (Optional)')
	last_msg = models.DateTimeField(blank=True, null=True)
	rate = models.IntegerField(null=True, blank=True, help_text="Uses the default rate unless otherwise specified.")

	objects = ReservationManager()

	@models.permalink
	def get_absolute_url(self):
		return ('core.views.ReservationDetail', [str(self.location.slug), str(self.id)])

	def __unicode__(self):
		return "reservation %d" % self.id

	def total_nights(self):
		return (self.depart - self.arrive).days
	total_nights.short_description = "Nights"

	def default_rate(self):
		# default_rate always returns the default rate regardless of comps or
		# custom rates.
		return self.room.default_rate

	def get_rate(self):
		if self.rate == None:
			return self.default_rate()
		return self.rate

	def total_value(self):
		# value of the reservation, regardless of what has been paid
		# get_rate checks for comps and custom rates.
		return self.total_nights() * self.get_rate()

	def total_owed(self):
		# Maybe someone was nice and they don't owe anything!
		if self.is_comped():
			return 0

		return self.bill_amount() - self.total_paid()

	def total_paid(self):
		payments = Payment.objects.filter(reservation=self)
		if not payments:
			return 0
		paid = Decimal(0)
		for payment in payments:
			paid = paid + payment.paid_amount
		return paid

	def fully_refunded(self):
		# says whether the reservation was fully refunded or not
		payments = Payment.objects.filter(reservation=self)
		if payments and sum([p.paid_amount for p in payments]) == 0:
			return True
		return False
		
	def bill_base_amount(self):
		# incorporates any manual discounts or fees into the base amount.
		# automatic fees are calculated on top of this. 
		base_fees = BillLineItem.objects.filter(reservation=self).filter(fee__isnull=True)
		return sum([item.amount for item in base_fees])
		
	def paid_date(self):
		if self.total_owed() == 0:
			# order by most recent first
			bill_payments = self.payments().order_by('-payment_date')
			return bill_payments[0].payment_date
		return None

	def total_owed_in_cents(self):
		# this is used to pass the information to stripe, which expects an
		# integer. 
		return int(self.total_owed() * 100)

	def calc_non_house_fees(self):
		# Calculate the amount of fees not paid by the house
		room_charge = self.total_value()
		amount = 0.0
		for location_fee in LocationFee.objects.filter(location = self.location):
			if not location_fee.fee.paid_by_house:
				amount = amount + (room_charge * location_fee.fee.percentage)
		return amount

	def calc_house_fees(self):
		# Calculate the amount of fees the house owes
		room_charge = self.total_value()
		amount = 0.0
		for location_fee in LocationFee.objects.filter(location = self.location):
			if location_fee.fee.paid_by_house:
				amount = amount + (room_charge * location_fee.fee.percentage)
		return amount

	def calc_bill_amount(self):
		total = 0
		for item in self.generate_bill(delete_old_items=False, save=False):
			if not item.paid_by_house:
				total = total + item.amount
		return total

	def generate_bill(self, delete_old_items=True, save=True):
		# impt! save the custom items first or they'll be blown away when the
		# bill is regenerated. 
		custom_items = list(BillLineItem.objects.filter(reservation=self).filter(custom=True))
		if delete_old_items:
			self.delete_bill()

		line_items = []

		# The first line item is for the room charge
		room_charge_desc = "%s (%d * $%d)" % (self.room.name, self.total_nights(), self.get_rate())
		room_charge = self.total_value()
		room_line_item = BillLineItem(reservation=self, description=room_charge_desc, amount=room_charge, paid_by_house=False)
		line_items.append(room_line_item)
		
		# Incorporate any custom fees or discounts
		effective_room_charge = room_charge
		for item in custom_items:
			line_items.append(item)
			effective_room_charge += item.amount #may be negative

		# A line item for every fee that applies to this location
		for location_fee in LocationFee.objects.filter(location = self.location):
			desc = "%s (%s%c)" % (location_fee.fee.description, (location_fee.fee.percentage * 100), '%')
			amount = float(effective_room_charge) * location_fee.fee.percentage
			fee_line_item = BillLineItem(reservation=self, description=desc, amount=amount, paid_by_house=location_fee.fee.paid_by_house, fee=location_fee.fee)
			line_items.append(fee_line_item)

		# Optionally save the line items to the database
		if save:
			for item in line_items:
				item.save()

		return line_items

	def delete_bill(self):
		BillLineItem.objects.filter(reservation=self).delete()

	def bill_amount(self):
		# Bill amount comes from generated bill line items
		amount = 0
		for line_item in BillLineItem.objects.filter(reservation=self):
			if not line_item.fee or not line_item.paid_by_house:
				amount = amount + line_item.amount
		return amount

	def house_fees(self):
		# Pull the house fees from the generated bill line items
		amount = 0
		for line_item in BillLineItem.objects.filter(reservation=self):
			if line_item.fee and line_item.paid_by_house:
				amount = amount + line_item.amount
		return amount

	def non_house_fees(self):
		# Pull the non-house fees from the generated bill line items
		amount = 0
		for line_item in BillLineItem.objects.filter(reservation=self):
			if line_item.fee and not line_item.paid_by_house:
				amount = amount + line_item.amount
		return amount

	def to_house(self):
		return self.total_value() - self.house_fees()

	def set_rate(self, rate):
		if rate == None:
			rate = 0
		self.rate = rate
		self.save()
		self.generate_bill()

	def reset_rate(self):
		self.set_rate(self.room.default_rate)

	def mark_last_msg(self):
		self.last_msg = datetime.datetime.now()
		self.save()

	def pending(self):
		self.status = Reservation.PENDING
		self.save()

	def approve(self):
		self.status = Reservation.APPROVED
		self.save()

	def confirm(self):
		self.status = Reservation.CONFIRMED
		self.save()

	def cancel(self):
		# cancel this reservation.
		# JKS note: we *don't* delete the bill here, because if there was a
		# refund, we want to keep it around to know how much to refund from the
		# associated fees. 
		self.status = Reservation.CANCELED
		self.save()

	def comp(self):
		self.set_rate(0)

	def is_paid(self):
		return self.total_owed() <= 0

	def is_comped(self):
		return self.rate == 0

	def is_pending(self):
		return self.status == Reservation.PENDING

	def is_approved(self):
		return self.status == Reservation.APPROVED

	def is_confirmed(self):
		return self.status == Reservation.CONFIRMED

	def is_canceled(self):
		return self.status == Reservation.CANCELED

	def payments(self):
		return Payment.objects.filter(reservation=self)

	def payment_date(self):
		# Date of the last payment
		payments = Payment.objects.filter(reservation=self).order_by('payment_date').reverse()
		if payments:
			payment = payments[0]
			if payment:
				return payment.payment_date

	def bill_line_items(self):
		# return bill line items orderer first with the room item, then the
		# custom items, then the fees
		items = BillLineItem.objects.filter(reservation=self)
		room_item = items.filter(custom=False).filter(fee=None)
		custom_items = items.filter(custom=True)
		fees = items.filter(fee__isnull=False)
		return list(room_item) + list(custom_items) + list(fees)

	def html_color_status(self):
		if self.is_paid():
			color_code = "#5fbf00"
		elif self.is_comped():
			color_code = "#ffc000"
		elif self.is_pending():
			color_code = "#bf0000"
		else:
			color_code = "#000000"
		return '<span style="color: %s;">%s</span>' % (color_code, self.status)
	html_color_status.allow_tags = True

class Payment(models.Model):
	reservation = models.ForeignKey(Reservation)
	payment_date = models.DateTimeField(auto_now_add=True)
	automatic_invoice = models.BooleanField(default=False, help_text="If True, an invoice will be sent to the user automatically at the end of their stay.")
	payment_service = models.CharField(max_length=200, blank=True, null=True, help_text="e.g., Stripe, Paypal, Dwolla, etc. May be empty")
	payment_method = models.CharField(max_length=200, blank=True, null=True, help_text="e.g., Visa, cash, bank transfer")
	paid_amount = models.DecimalField(max_digits=7, decimal_places=2, default=0)
	transaction_id = models.CharField(max_length=200, null=True, blank=True)

	def __unicode__(self):
		return "%s: %s - $%s" % (str(self.payment_date)[:16], self.reservation.user, self.paid_amount)

	def to_house(self):
		return self.paid_amount - self.non_house_fees() - self.house_fees()

	def non_house_fees(self):
		# takes the appropriate bill line items and applies them proportionately to the payment. 
		fee_line_items_not_paid_by_house = BillLineItem.objects.filter(reservation=self.reservation).filter(fee__isnull=False).filter(paid_by_house=False)
		base_amount = self.reservation.bill_base_amount()
		non_house_fee_on_payment = Decimal(0.0)
		# this payment may or may not represent the entire bill amount. we need
		# to know what fraction of the total bill amount it was so that we can
		# apply the fees proportionately to the payment amount. note: in many
		# cases, the fraction will be 1. 
		try:
			fraction = self.paid_amount/self.reservation.bill_amount()
		except:
			# if the bill amount is zero but there was a payment on the reservation, then the bill must be b0rked in some way
			self.reservation.generate_bill()
			fraction = self.paid_amount/self.reservation.bill_amount()

		fractional_base_amount = base_amount * fraction
		for line_item in fee_line_items_not_paid_by_house:
				# JKS important! this assumes that the line item value accurately
				# reflects the fee percentage. this should be true, but technically
				# could be edited in the admin page to be anything. do we want to
				# enforce this?
			non_house_fee_on_payment += fractional_base_amount * Decimal(line_item.fee.percentage)

		return non_house_fee_on_payment

	def house_fees(self):
		# takes the appropriate bill line items and applies them proportionately to the payment. 
		fee_line_items_paid_by_house = BillLineItem.objects.filter(reservation=self.reservation).filter(paid_by_house=True)
		base_amount = self.reservation.bill_base_amount()
		house_fee_on_payment = Decimal(0.0)
		# this payment may or may not represent the entire bill amount. we need
		# to know what fraction of the total bill amount it was so that we can
		# apply the fees proportionately to the payment amount. note: in many
		# cases, the fraction will be 1. 
		fraction = self.paid_amount/self.reservation.bill_amount()
		fractional_base_amount = base_amount * fraction
		for line_item in fee_line_items_paid_by_house:
			# JKS important! this assumes that the line item value accurately
			# reflects the fee percentage. this should be true, but technically
			# could be edited in the admin page to be anything. do we want to
			# enforce this?
			house_fee_on_payment += fractional_base_amount * Decimal(line_item.fee.percentage)
		return house_fee_on_payment


def profile_img_upload_to(instance, filename):
	ext = filename.split('.')[-1]
	# rename file to random string
	filename = "%s.%s" % (uuid.uuid4(), ext.lower())

	upload_path = "avatars/%s/" % instance.user.username
	upload_abs_path = os.path.join(settings.MEDIA_ROOT, upload_path)
	if not os.path.exists(upload_abs_path):
		os.makedirs(upload_abs_path)
	return os.path.join(upload_path, filename)

def get_default_profile_img():
	path = os.path.join(settings.MEDIA_ROOT, "avatars/default.jpg")
	return file(path)

class UserProfile(models.Model):
	IMG_SIZE = (300,300)
	IMG_THUMB_SIZE = (150,150)

	# User model fields: username, first_name, last_name, email,
	# password, is_staff, is_active, is_superuser, last_login, date_joined,
	user = models.OneToOneField(User)
	updated = models.DateTimeField(auto_now=True)
	image = models.ImageField(upload_to=profile_img_upload_to, help_text="Image should have square dimensions.")
	image_thumb = models.ImageField(upload_to="avatars/%Y/%m/%d/", blank=True, null=True)
	bio = models.TextField("About you", blank=True, null=True)
	links = models.TextField(help_text="Comma-separated", blank=True, null=True)

	projects = models.TextField(verbose_name='Current Projects', help_text='Describe one or more projects you are currently working on')
	sharing = models.TextField(help_text="Is there anything you'd be interested in learning or sharing during your stay?")
	discussion = models.TextField(help_text="We like discussing thorny issues with each other. What's a question that's been on your mind lately that you don't know the answer to?")
	referral = models.CharField(max_length=200, verbose_name='How did you hear about us? (Give a name if possible!)')
	city = models.CharField(max_length=200, verbose_name="In what city are you primarily based?")
	# currently used to store the stripe customer id but could be used for
	# other payment platforms in the future
	customer_id = models.CharField(max_length=200, blank=True, null=True)

	def __unicode__(self):
		return (self.user.__unicode__())

User.profile = property(lambda u: UserProfile.objects.get_or_create(user=u)[0])

User._meta.ordering = ['username']

@receiver(pre_save, sender=UserProfile)
def size_images(sender, instance, **kwargs):
	try:
		obj = UserProfile.objects.get(pk=instance.pk)
	except UserProfile.DoesNotExist:
		# if the reservation does not exist yet, then it's new.
		obj = None

	# if this is the default avatar, reuse it for the thumbnail (lazy, but only
	# for backwards compatibility for those who created accounts before images
	# were required)
	if instance.image.name == "avatars/default.jpg":
		instance.image_thumb = "avatars/default.thumb.jpg"

	elif instance.image and (obj == None or obj.image != instance.image or obj.image_thumb == None):
		im = Image.open(instance.image)

		img_upload_path_rel = profile_img_upload_to(instance, instance.image.name)
		main_img_full_path = os.path.join(settings.MEDIA_ROOT, img_upload_path_rel)
		# resize returns a copy. resize() forces the dimensions of the image
		# to match SIZE specified, squeezing the image if necessary along one
		# dimension.
		main_img = im.resize(UserProfile.IMG_SIZE, Image.ANTIALIAS)
		main_img.save(main_img_full_path)
		# the image field is a link to the path where the image is stored
		instance.image = img_upload_path_rel
		# now resize this to generate the smaller thumbnail
		thumb_img = im.resize(UserProfile.IMG_THUMB_SIZE, Image.ANTIALIAS)
		thumb_full_path = os.path.splitext(main_img_full_path)[0] + ".thumb" + os.path.splitext(main_img_full_path)[1]
		thumb_img.save(thumb_full_path)
		# the ImageFileField needs the path info relative to the media
		# directory
		# XXX Q: does this save the file twice? once by PIL and another time
		# reading it in and saving it to the same place when the model saves?
		thumb_rel_path = os.path.join(os.path.split(img_upload_path_rel)[0], os.path.basename(thumb_full_path))
		instance.image_thumb = thumb_rel_path

		# now delete any old images
		if obj and obj.image and obj.image.name != "avatars/default.jpg":
			default_storage.delete(obj.image.path)

		if obj and obj.image_thumb and obj.image_thumb.name != "avatars/default.thumb.jpg":
			default_storage.delete(obj.image_thumb.path)

class EmailTemplate(models.Model):
	''' Templates for the typical emails sent by administrators of the system. 
	The from-address is usually set by DEFAULT_FROM_ADDRESS in settings, 
	and the recipients are determined by the action and reservation in question. '''

	SUBJECT_PREFIX = settings.EMAIL_SUBJECT_PREFIX
	FROM_ADDRESS = settings.DEFAULT_FROM_EMAIL

	body = models.TextField(verbose_name="The body of the email")
	subject = models.CharField(max_length=200, verbose_name="Default Subject Line")
	name = models.CharField(max_length=200, verbose_name="Template Name")
	creator = models.ForeignKey(User)
	shared = models.BooleanField(default=False)

	def __unicode__(self):
		return self.name

class LocationEmailTemplate(models.Model):
	''' Location Template overrides for system generated emails '''
	
	ADMIN_DAILY = 'admin_daily_update'
	GUEST_DAILY = 'guest_daily_update'
	INVOICE = 'invoice'
	RECEIPT = 'receipt'
	NEW_RESERVATION = 'newreservation'
	WELCOME = 'pre_arrival_welcome'

	KEYS = (
			(ADMIN_DAILY, 'Admin Daily Update'),
			(GUEST_DAILY, 'Guest Daily Update'),
			(INVOICE, 'Invoice'),
			(RECEIPT, 'Receipt'),
			(NEW_RESERVATION, 'New Reservation'),
			(WELCOME, 'Pre-Arrival Welcome'),
		)

	location = models.ForeignKey(Location)
	key = models.CharField(max_length=32, choices=KEYS)
	text_body = models.TextField(verbose_name="The text body of the email")
	html_body = models.TextField(blank=True, null=True, verbose_name="The html body of the email")
	
class Fee(models.Model):
	description = models.CharField(max_length=100, verbose_name="Fee Name")
	percentage = models.FloatField(default=0, help_text="For example 5.2% = 0.052")
	paid_by_house = models.BooleanField(default=False)

	def __unicode__(self):
		return self.description

class LocationFee(models.Model):
	location = models.ForeignKey(Location)
	fee = models.ForeignKey(Fee)

	def __unicode__(self):
		return '%s: %s' % (self.location, self.fee)

class BillLineItem(models.Model):
	reservation = models.ForeignKey(Reservation)
	# the fee that this line item was based on, if any (line items are also
	# generated for the base room rate, which doesn't have an associated fee)
	fee = models.ForeignKey(Fee, null=True)
	description = models.CharField(max_length=200)
	# the actual amount of this line item (if this is a line item derived from
	# a fee, generally it will be the fee amount but, technically, not
	# necessarily)
	amount = models.DecimalField(max_digits=7, decimal_places=2, default=0)
	paid_by_house = models.BooleanField(default=True)
	custom = models.BooleanField(default=False)

	def __unicode__(self):
		return '%s: %s' % (self.reservation.location, self.description)

class LocationMenu(models.Model):
	location = models.ForeignKey(Location)
	name = models.CharField(max_length=15, help_text="A short title for your menu. Note: If there is only one page in the menu, it will be used as a top level nav item, and the menu name will not be used.")

	def page_count(self):
		return len(self.pages.all())

class LocationFlatPage(models.Model):
	menu = models.ForeignKey(LocationMenu, related_name = "pages", help_text="Note: If there is only one page in the menu, it will be used as a top level nav item, and the menu name will not be used.")
	flatpage = models.OneToOneField(FlatPage)
	
class Reservable(models.Model):
	room = models.ForeignKey(Room, related_name="reservables")
	start_date = models.DateField()
	end_date = models.DateField(null=True, blank=True)

	

