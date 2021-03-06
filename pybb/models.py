# -*- coding: utf-8 -*-
from __future__ import unicode_literals
import functools

from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.core.urlresolvers import reverse
from django.db.models.signals import post_delete, post_save
from django.db import models, transaction, DatabaseError
from django.utils.encoding import python_2_unicode_compatible
from django.utils.html import strip_tags
from django.utils.translation import ugettext_lazy as _
from django.utils.timezone import now as tznow

from pybb.compat import get_user_model_path, get_username_field, get_user_model, get_atomic_func
from pybb import defaults
from pybb.profiles import PybbProfile
from pybb.subscription import notify_topic_subscribers
from pybb.util import unescape, get_pybb_profile_model, get_pybb_profile, get_file_path

from annoying.fields import AutoOneToOneField

try:
    from south.modelsinspector import add_introspection_rules
    add_introspection_rules([], ["^annoying\.fields\.JSONField"])
    add_introspection_rules([], ["^annoying\.fields\.AutoOneToOneField"])
except ImportError:
    pass


@python_2_unicode_compatible
class Category(models.Model):
    name = models.CharField(_('Name'), max_length=80)
    position = models.IntegerField(_('Position'), blank=True, default=0)
    hidden = models.BooleanField(_('Hidden'), blank=False, null=False, default=False,
        help_text = _('If checked, this category will be visible only for staff')
    )

    class Meta(object):
        ordering = ['position']
        verbose_name = _('Category')
        verbose_name_plural = _('Categories')

    def __str__(self):
        return self.name

    def forum_count(self):
        return self.forums.all().count()

    def get_absolute_url(self):
        return reverse('pybb:category', kwargs={'pk': self.id})

    @property
    def topics(self):
        return Topic.objects.filter(forum__category=self).select_related()

    @property
    def posts(self):
        return Post.objects.filter(topic__forum__category=self).select_related()


@python_2_unicode_compatible
class Forum(models.Model):
    category = models.ForeignKey(Category, related_name='forums', verbose_name=_('Category'))
    parent = models.ForeignKey('self', related_name='child_forums', verbose_name=_('Parent forum'),
                               blank=True, null=True)
    name = models.CharField(_('Name'), max_length=80)
    position = models.IntegerField(_('Position'), blank=True, default=0)
    description = models.TextField(_('Description'), blank=True)
    moderators = models.ManyToManyField(get_user_model_path(), blank=True, null=True, verbose_name=_('Moderators'))
    updated = models.DateTimeField(_('Updated'), blank=True, null=True)
    post_count = models.IntegerField(_('Post count'), blank=True, default=0)
    topic_count = models.IntegerField(_('Topic count'), blank=True, default=0)
    hidden = models.BooleanField(_('Hidden'), blank=False, null=False, default=False)
    readed_by = models.ManyToManyField(get_user_model_path(), through='ForumReadTracker', related_name='readed_forums')
    headline = models.TextField(_('Headline'), blank=True, null=True)

    class Meta(object):
        ordering = ['position']
        verbose_name = _('Forum')
        verbose_name_plural = _('Forums')

    def __str__(self):
        return self.name

    def update_counters(self):
        posts = Post.objects.filter(topic__forum_id=self.id)
        self.post_count = posts.count()
        self.topic_count = Topic.objects.filter(forum=self).count()
        try:
            last_post = posts.order_by('-created', '-id')[0]
            self.updated = last_post.updated or last_post.created
        except IndexError:
            pass

        self.save()

    def get_absolute_url(self):
        return reverse('pybb:forum', kwargs={'pk': self.id})

    @property
    def posts(self):
        return Post.objects.filter(topic__forum=self).select_related()

    @property
    def last_post(self):
        try:
            return self.posts.order_by('-created', '-id')[0]
        except IndexError:
            return None

    def get_parents(self):
        """
        Used in templates for breadcrumb building
        """
        parents = [self.category]
        parent = self.parent
        while parent is not None:
            parents.insert(1, parent)
            parent = parent.parent
        return parents


@python_2_unicode_compatible
class Topic(models.Model):
    POLL_TYPE_NONE = 0
    POLL_TYPE_SINGLE = 1
    POLL_TYPE_MULTIPLE = 2

    POLL_TYPE_CHOICES = (
        (POLL_TYPE_NONE, _('None')),
        (POLL_TYPE_SINGLE, _('Single answer')),
        (POLL_TYPE_MULTIPLE, _('Multiple answers')),
    )

    forum = models.ForeignKey(Forum, related_name='topics', verbose_name=_('Forum'))
    name = models.CharField(_('Subject'), max_length=255)
    created = models.DateTimeField(_('Created'), null=True)
    updated = models.DateTimeField(_('Updated'), null=True)
    user = models.ForeignKey(get_user_model_path(), verbose_name=_('User'))
    views = models.IntegerField(_('Views count'), blank=True, default=0)
    sticky = models.BooleanField(_('Sticky'), blank=True, default=False)
    closed = models.BooleanField(_('Closed'), blank=True, default=False)
    subscribers = models.ManyToManyField(get_user_model_path(), related_name='subscriptions', verbose_name=_('Subscribers'),
        blank=True)
    post_count = models.IntegerField(_('Post count'), blank=True, default=0)
    readed_by = models.ManyToManyField(get_user_model_path(), through='TopicReadTracker', related_name='readed_topics')
    on_moderation = models.BooleanField(_('On moderation'), default=False)
    poll_type = models.IntegerField(_('Poll type'), choices=POLL_TYPE_CHOICES, default=POLL_TYPE_NONE)
    poll_question = models.TextField(_('Poll question'), blank=True, null=True)

    class Meta(object):
        ordering = ['-created']
        verbose_name = _('Topic')
        verbose_name_plural = _('Topics')

    def __str__(self):
        return self.name

    @property
    def head(self):
        """
        Get first post and cache it for request
        """
        if not hasattr(self, "_head"):
            self._head = self.posts.all().order_by('created', 'id')
        if not len(self._head):
            return None
        return self._head[0]

    @property
    def last_post(self):
        if not getattr(self, '_last_post', None):
            self._last_post = self.posts.order_by('-created', '-id').select_related('user')[0]
        return self._last_post

    def get_absolute_url(self):
        return reverse('pybb:topic', kwargs={'pk': self.id})

    def save(self, *args, **kwargs):
        if self.id is None:
            self.created = tznow()

        forum_changed = False
        old_topic = None
        if self.id is not None:
            old_topic = Topic.objects.get(id=self.id)
            if self.forum != old_topic.forum:
                forum_changed = True

        super(Topic, self).save(*args, **kwargs)

        if forum_changed:
            old_topic.forum.update_counters()
            self.forum.update_counters()

    def delete(self, using=None):
        super(Topic, self).delete(using)
        self.forum.update_counters()

    def update_counters(self):
        self.post_count = self.posts.count()
        last_post = Post.objects.filter(topic_id=self.id).order_by('-created', '-id')[0]
        self.updated = last_post.updated or last_post.created
        self.save()

    def get_parents(self):
        """
        Used in templates for breadcrumb building
        """
        parents = self.forum.get_parents()
        parents.append(self.forum)
        return parents

    def poll_votes(self):
        if self.poll_type != self.POLL_TYPE_NONE:
            return PollAnswerUser.objects.filter(poll_answer__topic=self).count()
        else:
            return None


class RenderableItem(models.Model):
    """
    Base class for models that has markup, body, body_text and body_html fields.
    """

    class Meta(object):
        abstract = True

    body = models.TextField(_('Message'))
    body_html = models.TextField(_('HTML version'))
    body_text = models.TextField(_('Text version'))

    def render(self):
        self.body_html = defaults.PYBB_MARKUP_ENGINES[defaults.PYBB_MARKUP](self.body)
        # Remove tags which was generated with the markup processor
        text = strip_tags(self.body_html)
        # Unescape entities which was generated with the markup processor
        self.body_text = unescape(text)


@python_2_unicode_compatible
class Post(RenderableItem):
    topic = models.ForeignKey(Topic, related_name='posts', verbose_name=_('Topic'))
    user = models.ForeignKey(get_user_model_path(), related_name='posts', verbose_name=_('User'))
    created = models.DateTimeField(_('Created'), blank=True, db_index=True)
    updated = models.DateTimeField(_('Updated'), blank=True, null=True)
    user_ip = models.IPAddressField(_('User IP'), blank=True, default='0.0.0.0')
    on_moderation = models.BooleanField(_('On moderation'), default=False)

    class Meta(object):
        ordering = ['created']
        verbose_name = _('Post')
        verbose_name_plural = _('Posts')

    def summary(self):
        LIMIT = 50
        tail = len(self.body) > LIMIT and '...' or ''
        return self.body[:LIMIT] + tail

    def __str__(self):
        return self.summary()

    def save(self, *args, **kwargs):
        created_at = tznow()
        if self.created is None:
            self.created = created_at
        self.render()

        new = self.pk is None

        topic_changed = False
        old_post = None
        if not new:
            old_post = Post.objects.get(pk=self.pk)
            if old_post.topic != self.topic:
                topic_changed = True

        super(Post, self).save(*args, **kwargs)

        # If post is topic head and moderated, moderate topic too
        if self.topic.head == self and not self.on_moderation and self.topic.on_moderation:
            self.topic.on_moderation = False

        self.topic.update_counters()
        self.topic.forum.update_counters()

        if topic_changed:
            old_post.topic.update_counters()
            old_post.topic.forum.update_counters()

    def get_absolute_url(self):
        return reverse('pybb:post', kwargs={'pk': self.id})

    def delete(self, *args, **kwargs):
        self_id = self.id
        head_post_id = self.topic.posts.order_by('created', 'id')[0].id

        if self_id == head_post_id:
            self.topic.delete()
        else:
            super(Post, self).delete(*args, **kwargs)
            self.topic.update_counters()
            self.topic.forum.update_counters()

    def get_parents(self):
        """
        Used in templates for breadcrumb building
        """
        return self.topic.forum.category, self.topic.forum, self.topic,


class Profile(PybbProfile):
    """
    Profile class that can be used if you doesn't have
    your site profile.
    """
    user = AutoOneToOneField(get_user_model_path(), related_name='pybb_profile', verbose_name=_('User'))

    class Meta(object):
        verbose_name = _('Profile')
        verbose_name_plural = _('Profiles')

    def get_absolute_url(self):
        return reverse('pybb:user', kwargs={'username': getattr(self.user, get_username_field())})


class Attachment(models.Model):

    class Meta(object):
        verbose_name = _('Attachment')
        verbose_name_plural = _('Attachments')

    post = models.ForeignKey(Post, verbose_name=_('Post'), related_name='attachments')
    size = models.IntegerField(_('Size'))
    file = models.FileField(_('File'),
                            upload_to=functools.partial(get_file_path, to=defaults.PYBB_ATTACHMENT_UPLOAD_TO))

    def save(self, *args, **kwargs):
        self.size = self.file.size
        super(Attachment, self).save(*args, **kwargs)

    def size_display(self):
        size = self.size
        if size < 1024:
            return '%db' % size
        elif size < 1024 * 1024:
            return '%dKb' % int(size / 1024)
        else:
            return '%.2fMb' % (size / float(1024 * 1024))


class TopicReadTrackerManager(models.Manager):
    def get_or_create_tracker(self, user, topic):
        """
        Correctly create tracker in mysql db on default REPEATABLE READ transaction mode

        It's known problem when standrard get_or_create method return can raise exception
        with correct data in mysql database.
        See http://stackoverflow.com/questions/2235318/how-do-i-deal-with-this-race-condition-in-django/2235624
        """
        is_new = True
        sid = transaction.savepoint(using=self.db)
        try:
            with get_atomic_func()():
                obj = TopicReadTracker.objects.create(user=user, topic=topic)
            transaction.savepoint_commit(sid)
        except DatabaseError:
            transaction.savepoint_rollback(sid)
            obj = TopicReadTracker.objects.get(user=user, topic=topic)
            is_new = False
        return obj, is_new


class TopicReadTracker(models.Model):
    """
    Save per user topic read tracking
    """
    user = models.ForeignKey(get_user_model_path(), blank=False, null=False)
    topic = models.ForeignKey(Topic, blank=True, null=True)
    time_stamp = models.DateTimeField(auto_now=True)

    objects = TopicReadTrackerManager()

    class Meta(object):
        verbose_name = _('Topic read tracker')
        verbose_name_plural = _('Topic read trackers')
        unique_together = ('user', 'topic')


class ForumReadTrackerManager(models.Manager):
    def get_or_create_tracker(self, user, forum):
        """
        Correctly create tracker in mysql db on default REPEATABLE READ transaction mode

        It's known problem when standrard get_or_create method return can raise exception
        with correct data in mysql database.
        See http://stackoverflow.com/questions/2235318/how-do-i-deal-with-this-race-condition-in-django/2235624
        """
        is_new = True
        sid = transaction.savepoint(using=self.db)
        try:
            with get_atomic_func()():
                obj = ForumReadTracker.objects.create(user=user, forum=forum)
            transaction.savepoint_commit(sid)
        except DatabaseError:
            transaction.savepoint_rollback(sid)
            is_new = False
            obj = ForumReadTracker.objects.get(user=user, forum=forum)
        return obj, is_new


class ForumReadTracker(models.Model):
    """
    Save per user forum read tracking
    """
    user = models.ForeignKey(get_user_model_path(), blank=False, null=False)
    forum = models.ForeignKey(Forum, blank=True, null=True)
    time_stamp = models.DateTimeField(auto_now=True)

    objects = ForumReadTrackerManager()

    class Meta(object):
        verbose_name = _('Forum read tracker')
        verbose_name_plural = _('Forum read trackers')
        unique_together = ('user', 'forum')


@python_2_unicode_compatible
class PollAnswer(models.Model):
    topic = models.ForeignKey(Topic, related_name='poll_answers', verbose_name=_('Topic'))
    text = models.CharField(max_length=255, verbose_name=_('Text'))

    class Meta:
        verbose_name = _('Poll answer')
        verbose_name_plural = _('Polls answers')

    def __str__(self):
        return self.text

    def votes(self):
        return self.users.count()

    def votes_percent(self):
        topic_votes = self.topic.poll_votes()
        if topic_votes > 0:
            return 1.0 * self.votes() / topic_votes * 100
        else:
            return 0


@python_2_unicode_compatible
class PollAnswerUser(models.Model):
    poll_answer = models.ForeignKey(PollAnswer, related_name='users', verbose_name=_('Poll answer'))
    user = models.ForeignKey(get_user_model_path(), related_name='poll_answers', verbose_name=_('User'))
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('Poll answer user')
        verbose_name_plural = _('Polls answers users')
        unique_together = (('poll_answer', 'user', ), )

    def __str__(self):
        return '%s - %s' % (self.poll_answer.topic, self.user)


def post_saved(instance, **kwargs):
    notify_topic_subscribers(instance)

    if get_pybb_profile(instance.user).autosubscribe:
        instance.topic.subscribers.add(instance.user)

    if kwargs['created']:
        profile = get_pybb_profile(instance.user)
        profile.post_count = instance.user.posts.count()
        profile.save()


def post_deleted(instance, **kwargs):
    profile = get_pybb_profile(instance.user)
    profile.post_count = instance.user.posts.count()
    profile.save()


def user_saved(instance, created, **kwargs):
    if not created:
        return
    try:
        add_post_permission = Permission.objects.get_by_natural_key('add_post', 'pybb', 'post')
        add_topic_permission = Permission.objects.get_by_natural_key('add_topic', 'pybb', 'topic')
    except (Permission.DoesNotExist, ContentType.DoesNotExist):
        return
    instance.user_permissions.add(add_post_permission, add_topic_permission)
    instance.save()
    if get_pybb_profile_model() == Profile:
        Profile(user=instance).save()


post_save.connect(post_saved, sender=Post)
post_delete.connect(post_deleted, sender=Post)
if defaults.PYBB_AUTO_USER_PERMISSIONS:
    post_save.connect(user_saved, sender=get_user_model())
