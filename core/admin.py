from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import User

from core.models import *
from gather.models import EventAdminGroup
from core.emails import *

# TODO - Needs to be locked down based on location.
# http://reinout.vanrees.org/weblog/2011/09/30/django-admin-filtering.html

class EmailTemplateAdmin(admin.ModelAdmin):
	model = EmailTemplate
	exclude = ('creator',)
	def save_model(self, request, obj, form, change): 
		obj.creator = request.user 
		obj.save() 

class LocationEmailTemplateAdmin(admin.ModelAdmin):
	model = LocationEmailTemplate
	list_display=('location', 'key')

class EventAdminGroupInline(admin.TabularInline):
	model = EventAdminGroup
	filter_horizontal = ['users',]

class ReservableAdminInline(admin.TabularInline):
	model = Reservable

class RoomAdmin(admin.ModelAdmin):
	model = Room
	inlines = [ReservableAdminInline]

class RoomAdminInline(admin.TabularInline):
	model = Room
	extra = 0

class LocationAdmin(admin.ModelAdmin):
	def send_admin_daily_update(self, request, queryset):
		for res in queryset:
			admin_daily_update(res)
	 	msg = gen_message(queryset, "email", "emails", "sent")
		self.message_user(request, msg)

	def send_guest_daily_update(self, request, queryset):
		for res in queryset:
			guest_daily_update(res)
	 	msg = gen_message(queryset, "email", "emails", "sent")
		self.message_user(request, msg)

	model=Location
	save_as = True
	list_display=('name', 'address')
	list_filter=('name',)
	filter_horizontal = ['residents', 'house_admins']
	actions= ['send_admin_daily_update', 'send_guest_daily_update']

	inlines = [RoomAdminInline]
	if 'gather' in settings.INSTALLED_APPS:
		 inlines.append(EventAdminGroupInline)

class PaymentAdmin(admin.ModelAdmin):
	def user(self):
		return '''<a href="/people/%s">%s %s</a> (%s)''' % (self.reservation.user.username, self.reservation.user.first_name, self.reservation.user.last_name, self.reservation.user.username)
	user.allow_tags = True

	def reservation(self):
		return '''<a href="/locations/%s/reservation/%s/">%s''' % (self.reservation.location.slug, self.reservation.id, self.reservation)
	reservation.allow_tags = True

	model=Payment
	list_display=('payment_date', user,  reservation, 'payment_method', 'paid_amount')
	list_filter = ('payment_method',)
	ordering = ['-payment_date',]

class PaymentInline(admin.TabularInline):
	model = Payment
	extra = 0

class BillLineItemAdmin(admin.ModelAdmin):
	def user(self):
		return '''<a href="/people/%s">%s %s</a> (%s)''' % (self.reservation.user.username, self.reservation.user.first_name, self.reservation.user.last_name, self.reservation.user.username)
	user.allow_tags = True

	def location(self):
		return self.reservation.location

	list_display = ('id', 'reservation', user, location, 'description', 'amount', 'paid_by_house')
	list_filter = ('fee', 'paid_by_house', 'reservation__location')

class BillLineItemInline(admin.TabularInline):
	model = BillLineItem
	fields = ('fee', 'description', 'amount', 'paid_by_house')
	readonly_fields = ('fee',)
	extra = 0

def gen_message(queryset, noun, pl_noun, suffix):
	if len(queryset) == 1:
		prefix = "1 %s was" % noun
	else:
		prefix = "%d %s were" % (len(queryset), pl_noun)
	msg = prefix + " " + suffix + "."
	return msg

class ReservationAdmin(admin.ModelAdmin):
	def rate(self):
		if self.rate == None:
			return None
		return "$%d" % self.rate

	def value(self):
		return "$%d" % self.total_value()

	def bill(self):
		return "$%d" % self.bill_amount()

	def fees(self):
		return "$%d" % self.non_house_fees()

	def to_house(self):
		return "$%d" % self.to_house()
		
	def paid(self):
		return "$%d" % self.total_paid()

	def user_profile(self):
		return '''<a href="/people/%s">%s %s</a> (%s)''' % (self.user.username, self.user.first_name, self.user.last_name, self.user.username)
	user_profile.allow_tags = True

	def send_receipt(self, request, queryset):
		success_list = []
		failure_list = []
		for res in queryset:
			if send_receipt(res):
				success_list.append(str(res.id))
			else:
				failure_list.append(str(res.id))
		msg = ""
		if len(success_list) > 0:
			msg += "Receipts sent for reservation(s) %s. " % ",".join(success_list)
		if len(failure_list) > 0:
			msg += "Receipt sending failed for reservation(s) %s. (Make sure all payment information has been entered in the reservation details and that the status of the reservation is either unpaid or paid.)" % ",".join(failure_list)
		self.message_user(request, msg)

	def send_invoice(self, request, queryset):
		for res in queryset:
			send_invoice(res)
	 	msg = gen_message(queryset, "invoice", "invoices", "sent")
		self.message_user(request, msg)

	def send_new_reservation_notify(self, request, queryset):
		for res in queryset:
			new_reservation_notify(res)
	 	msg = gen_message(queryset, "email", "emails", "sent")
		self.message_user(request, msg)

	def send_updated_reservation_notify(self, request, queryset):
		for res in queryset:
			updated_reservation_notify(res)
	 	msg = gen_message(queryset, "email", "emails", "sent")
		self.message_user(request, msg)

	def send_guest_welcome(self, request, queryset):
		for res in queryset:
			guest_welcome(res)
	 	msg = gen_message(queryset, "email", "emails", "sent")
		self.message_user(request, msg)

	def mark_as_comp(self, request, queryset):
		for res in queryset:
			res.comp()
	 	msg = gen_message(queryset, "reservation", "reservations", "marked as comp")
		self.message_user(request, msg)

	def revert_to_pending(self, request, queryset):
		for res in queryset:
			res.pending()
		msg = gen_message(queryset, "reservation", "reservations", "reverted to pending")
		self.message_user(request, msg)

	def approve(self, request, queryset):
		for res in queryset:
			res.approve()
		msg = gen_message(queryset, "reservation", "reservations", "approved")
		self.message_user(request, msg)

	def confirm(self, request, queryset):
		for res in queryset:
			res.confirm()
		msg = gen_message(queryset, "reservation", "reservations", "confirmed")
		self.message_user(request, msg)

	def cancel(self, request, queryset):
		for res in queryset:
			res.cancel()
		msg = gen_message(queryset, "reservation", "reservations", "canceled")
		self.message_user(request, msg)

	def reset_rate(self, request, queryset):
		for res in queryset:
			res.reset_rate()
		msg = gen_message(queryset, "reservation", "reservations", "set to default rate")
		self.message_user(request, msg)

	def recalculate_bill(self, request, queryset):
		for res in queryset:
			res.generate_bill()
		msg = gen_message(queryset, "bill", "bills", "recalculated")
		self.message_user(request, msg)

	model = Reservation
	list_filter = ('status', 'location')
	list_display = ('id', user_profile, 'status', 'arrive', 'depart', 'room', 'total_nights', rate, fees, bill, to_house, paid )
	#list_editable = ('status',) # Depricated in favor of drop down actions
	search_fields = ('user__username', 'user__first_name', 'user__last_name', 'id')
	inlines = [BillLineItemInline, PaymentInline]
	ordering = ['-arrive', 'id']
	actions= ['send_guest_welcome', 'send_new_reservation_notify', 'send_updated_reservation_notify', 'send_receipt', 'send_invoice', 'recalculate_bill', 'mark_as_comp', 'reset_rate', 'revert_to_pending', 'approve', 'confirm', 'cancel']
	save_as = True
	
class UserProfileInline(admin.StackedInline):
	model = UserProfile
 
class UserProfileAdmin(UserAdmin):
	inlines = [UserProfileInline]
	list_display = ('username', 'email', 'first_name', 'last_name', 'is_staff', 'date_joined', 'last_login')

class LocationFlatPageInline(admin.StackedInline):
	model = LocationFlatPage

class LocationMenuAdmin(admin.ModelAdmin):
	model = LocationMenu
	inlines = [LocationFlatPageInline]
	list_display = ('location', 'name')

admin.site.register(LocationMenu, LocationMenuAdmin)
admin.site.register(Reservation, ReservationAdmin)
admin.site.register(Room, RoomAdmin)
admin.site.register(Location, LocationAdmin)
admin.site.register(Payment, PaymentAdmin)
admin.site.register(EmailTemplate, EmailTemplateAdmin)
admin.site.register(LocationEmailTemplate, LocationEmailTemplateAdmin)
admin.site.register(BillLineItem, BillLineItemAdmin)

admin.site.unregister(User)
admin.site.register(User, UserProfileAdmin)

admin.site.register(Fee)
admin.site.register(LocationFee)
