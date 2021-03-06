from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.utils.translation import ugettext as _, ugettext_lazy, ungettext

from misago.acl import add_acl
from misago.categories import THREADS_ROOT_NAME
from misago.conf import settings
from misago.threads.mergeconflict import MergeConflict
from misago.threads.models import Thread
from misago.threads.permissions import (
    allow_delete_best_answer, allow_delete_event, allow_delete_post, allow_delete_thread,
    allow_merge_post, allow_merge_thread,
    allow_move_post, allow_split_post,
    can_reply_thread, can_see_thread,
    can_start_thread, exclude_invisible_posts)
from misago.threads.threadtypes import trees_map
from misago.threads.utils import get_thread_id_from_url
from misago.threads.validators import validate_category, validate_title


POSTS_LIMIT = settings.MISAGO_POSTS_PER_PAGE + settings.MISAGO_POSTS_TAIL
THREADS_LIMIT = settings.MISAGO_THREADS_PER_PAGE + settings.MISAGO_THREADS_TAIL


class DeletePostsSerializer(serializers.Serializer):
    error_empty_or_required = ugettext_lazy("You have to specify at least one post to delete.")

    posts = serializers.ListField(
        allow_empty=False,
        child=serializers.IntegerField(
            error_messages={
                'invalid': ugettext_lazy("One or more post ids received were invalid."),
            },
        ),
        error_messages={
            'required': error_empty_or_required,
            'null': error_empty_or_required,
            'empty': error_empty_or_required,
        },
    )

    def validate_posts(self, data):
        if len(data) > POSTS_LIMIT:
            message = ungettext(
                "No more than %(limit)s post can be deleted at single time.",
                "No more than %(limit)s posts can be deleted at single time.",
                POSTS_LIMIT,
            )
            raise ValidationError(message % {'limit': POSTS_LIMIT})

        user = self.context['user']
        thread = self.context['thread']

        posts_queryset = exclude_invisible_posts(user, thread.category, thread.post_set)
        posts_queryset = posts_queryset.filter(id__in=data).order_by('id')

        posts = []
        for post in posts_queryset:
            post.category = thread.category
            post.thread = thread

            if post.is_event:
                allow_delete_event(user, post)
            else:
                allow_delete_best_answer(user, post)
                allow_delete_post(user, post)

            posts.append(post)

        if len(posts) != len(data):
            raise PermissionDenied(_("One or more posts to delete could not be found."))

        return posts


class MergePostsSerializer(serializers.Serializer):
    error_empty_or_required = ugettext_lazy("You have to select at least two posts to merge.")

    posts = serializers.ListField(
        child=serializers.IntegerField(
            error_messages={
                'invalid': ugettext_lazy("One or more post ids received were invalid."),
            },
        ),
        error_messages={
            'null': error_empty_or_required,
            'required': error_empty_or_required,
        },
    )

    def validate_posts(self, data):
        data = list(set(data))

        if len(data) < 2:
            raise ValidationError(self.error_empty_or_required)
        if len(data) > POSTS_LIMIT:
            message = ungettext(
                "No more than %(limit)s post can be merged at single time.",
                "No more than %(limit)s posts can be merged at single time.",
                POSTS_LIMIT,
            )
            raise ValidationError(message % {'limit': POSTS_LIMIT})

        user = self.context['user']
        thread = self.context['thread']

        posts_queryset = exclude_invisible_posts(user, thread.category, thread.post_set)
        posts_queryset = posts_queryset.filter(id__in=data).order_by('id')

        posts = []
        for post in posts_queryset:
            post.category = thread.category
            post.thread = thread

            try:
                allow_merge_post(user, post)
            except PermissionDenied as e:
                raise ValidationError(e)

            if not posts:
                posts.append(post)
                continue
            
            authorship_error = _("Posts made by different users can't be merged.")
            if post.poster_id != posts[0].poster_id:
                raise serializers.ValidationError(authorship_error)
            elif (post.poster_id is None and posts[0].poster_id is None and 
                    post.poster_name != posts[0].poster_name):
                raise serializers.ValidationError(authorship_error)

            if posts[0].is_first_post and post.is_best_answer:
                raise serializers.ValidationError(
                    _("Post marked as best answer can't be merged with thread's first post.")
                )

            if not posts[0].is_first_post:
                if (posts[0].is_hidden != post.is_hidden or
                        posts[0].is_unapproved != post.is_unapproved):
                    raise serializers.ValidationError(
                        _("Posts with different visibility can't be merged.")
                    )

            posts.append(post)

        if len(posts) != len(data):
            raise ValidationError(_("One or more posts to merge could not be found."))

        return posts


class MovePostsSerializer(serializers.Serializer):
    error_empty_or_required = ugettext_lazy("You have to specify at least one post to move.")

    new_thread = serializers.CharField(
        error_messages={
            'required': ugettext_lazy("Enter link to new thread."),
        },
    )
    posts = serializers.ListField(
        allow_empty=False,
        child=serializers.IntegerField(
            error_messages={
                'invalid': ugettext_lazy("One or more post ids received were invalid."),
            },
        ),
        error_messages={
            'empty': error_empty_or_required,
            'null': error_empty_or_required,
            'required': error_empty_or_required,
        },
    )

    def validate_new_thread(self, data):
        request = self.context['request']
        thread = self.context['thread']
        viewmodel = self.context['viewmodel']

        new_thread_id = get_thread_id_from_url(request, data)
        if not new_thread_id:
            raise ValidationError(_("This is not a valid thread link."))
        if new_thread_id == thread.pk:
            raise ValidationError(_("Thread to move posts to is same as current one."))

        try:
            new_thread = viewmodel(request, new_thread_id).unwrap()
        except Http404:
            raise ValidationError(
                _(
                    "The thread you have entered link to doesn't "
                    "exist or you don't have permission to see it."
                )
            )

        if not new_thread.acl['can_reply']:
            raise ValidationError(_("You can't move posts to threads you can't reply."))

        return new_thread

    def validate_posts(self, data):
        data = list(set(data))
        if len(data) > POSTS_LIMIT:
            message = ungettext(
                "No more than %(limit)s post can be moved at single time.",
                "No more than %(limit)s posts can be moved at single time.",
                POSTS_LIMIT,
            )
            raise ValidationError(message % {'limit': POSTS_LIMIT})

        request = self.context['request']
        thread = self.context['thread']

        posts_queryset = exclude_invisible_posts(request.user, thread.category, thread.post_set)
        posts_queryset = posts_queryset.filter(id__in=data).order_by('id')

        posts = []
        for post in posts_queryset:
            post.category = thread.category
            post.thread = thread

            try:
                allow_move_post(request.user, post)
                posts.append(post)
            except PermissionDenied as e:
                raise ValidationError(e)

        if len(posts) != len(data):
            raise ValidationError(_("One or more posts to move could not be found."))

        return posts


class NewThreadSerializer(serializers.Serializer):
    title = serializers.CharField()
    category = serializers.IntegerField()
    weight = serializers.IntegerField(
        required=False,
        allow_null=True,
        max_value=Thread.WEIGHT_GLOBAL,
        min_value=Thread.WEIGHT_DEFAULT,
    )
    is_hidden = serializers.NullBooleanField(required=False)
    is_closed = serializers.NullBooleanField(required=False)

    def validate_title(self, title):
        return validate_title(title)

    def validate_category(self, category_id):
        self.category = validate_category(self.context['user'], category_id)
        if not can_start_thread(self.context['user'], self.category):
            raise ValidationError(
                _("You can't create new threads in selected category."))
        return self.category

    def validate_weight(self, weight):
        try:
            add_acl(self.context['user'], self.category)
        except AttributeError:
            return weight  # don't validate weight further if category failed

        if weight > self.category.acl.get('can_pin_threads', 0):
            if weight == 2:
                raise ValidationError(
                    _("You don't have permission to pin threads globally in this category.")
                )
            else:
                raise ValidationError(
                    _("You don't have permission to pin threads in this category.")
                )
        return weight

    def validate_is_hidden(self, is_hidden):
        try:
            add_acl(self.context['user'], self.category)
        except AttributeError:
            return is_hidden  # don't validate hidden further if category failed

        if is_hidden and not self.category.acl.get('can_hide_threads'):
            raise ValidationError(
                _("You don't have permission to hide threads in this category."))
        return is_hidden

    def validate_is_closed(self, is_closed):
        try:
            add_acl(self.context['user'], self.category)
        except AttributeError:
            return is_closed  # don't validate closed further if category failed

        if is_closed and not self.category.acl.get('can_close_threads'):
            raise ValidationError(
                _("You don't have permission to close threads in this category.")
            )
        return is_closed


class SplitPostsSerializer(NewThreadSerializer):
    error_empty_or_required = ugettext_lazy("You have to specify at least one post to split.")

    posts = serializers.ListField(
        allow_empty=False,
        child=serializers.IntegerField(
            error_messages={
                'invalid': ugettext_lazy("One or more post ids received were invalid."),
            },
        ),
        error_messages={
            'empty': error_empty_or_required,
            'null': error_empty_or_required,
            'required': error_empty_or_required,
        },
    )

    def validate_posts(self, data):
        if len(data) > POSTS_LIMIT:
            message = ungettext(
                "No more than %(limit)s post can be split at single time.",
                "No more than %(limit)s posts can be split at single time.",
                POSTS_LIMIT,
            )
            raise ValidationError(message % {'limit': POSTS_LIMIT})

        thread = self.context['thread']
        user = self.context['user']

        posts_queryset = exclude_invisible_posts(user, thread.category, thread.post_set)
        posts_queryset = posts_queryset.filter(id__in=data).order_by('id')

        posts = []
        for post in posts_queryset:
            post.category = thread.category
            post.thread = thread

            try:
                allow_split_post(user, post)
            except PermissionDenied as e:
                raise ValidationError(e)

            posts.append(post)

        if len(posts) != len(data):
            raise ValidationError(_("One or more posts to split could not be found."))

        return posts


class DeleteThreadsSerializer(serializers.Serializer):
    error_empty_or_required = ugettext_lazy("You have to specify at least one thread to delete.")

    threads = serializers.ListField(
        allow_empty=False,
        child=serializers.IntegerField(
            error_messages={
                'invalid': ugettext_lazy("One or more thread ids received were invalid."),
            },
        ),
        error_messages={
            'required': error_empty_or_required,
            'null': error_empty_or_required,
            'empty': error_empty_or_required,
        },
    )

    def validate_threads(self, data):
        if len(data) > THREADS_LIMIT:
            message = ungettext(
                "No more than %(limit)s thread can be deleted at single time.",
                "No more than %(limit)s threads can be deleted at single time.",
                THREADS_LIMIT,
            )
            raise ValidationError(message % {'limit': THREADS_LIMIT})

        request = self.context['request']
        viewmodel = self.context['viewmodel']

        threads = []
        errors = []

        sorted_ids = sorted(data, reverse=True)

        for thread_id in sorted_ids:
            try:
                thread = viewmodel(request, thread_id).unwrap()
                allow_delete_thread(request.user, thread)
                threads.append(thread)
            except PermissionDenied as permission_error:
                errors.append({
                    'thread': {
                        'id': thread.id,
                        'title': thread.title
                    },
                    'error': permission_error,
                })
            except Http404 as e:
                pass # skip invisible threads

        if errors:
            raise ValidationError({'details': errors})

        if len(threads) != len(data):
            raise ValidationError(_("One or more threads to delete could not be found."))

        return threads


class MergeThreadSerializer(serializers.Serializer):
    other_thread = serializers.CharField(
        error_messages={
            'required': ugettext_lazy("Enter link to new thread."),
        },
    )
    best_answer = serializers.IntegerField(
        required=False,
        error_messages={
            'invalid': ugettext_lazy("Invalid choice."),
        },
    )
    poll = serializers.IntegerField(
        required=False,
        error_messages={
            'invalid': ugettext_lazy("Invalid choice."),
        },
    )

    def validate_other_thread(self, data):
        request = self.context['request']
        thread = self.context['thread']
        viewmodel = self.context['viewmodel']

        other_thread_id = get_thread_id_from_url(request, data)
        if not other_thread_id:
            raise ValidationError(_("This is not a valid thread link."))
        if other_thread_id == thread.pk:
            raise ValidationError(_("You can't merge thread with itself."))

        try:
            other_thread = viewmodel(request, other_thread_id).unwrap()
            allow_merge_thread(request.user, other_thread, otherthread=True)
        except PermissionDenied as e:
            raise ValidationError(e)
        except Http404:
            raise ValidationError(
                _(
                    "The thread you have entered link to doesn't "
                    "exist or you don't have permission to see it."
                )
            )

        if not can_reply_thread(request.user, other_thread):
            raise ValidationError(_("You can't merge this thread into thread you can't reply."))

        return other_thread

    def validate(self, data):
        thread = self.context['thread']
        other_thread = data['other_thread']

        merge_conflict = MergeConflict(data, [thread, other_thread])
        merge_conflict.is_valid(raise_exception=True)
        data.update(merge_conflict.get_resolution())
        self.merge_conflict = merge_conflict.get_conflicting_fields()

        return data


class MergeThreadsSerializer(NewThreadSerializer):
    error_empty_or_required = ugettext_lazy("You have to select at least two threads to merge.")

    threads = serializers.ListField(
        allow_empty=False,
        min_length=2,
        child=serializers.IntegerField(
            error_messages={
                'invalid': ugettext_lazy("One or more thread ids received were invalid."),
            },
        ),
        error_messages={
            'empty': error_empty_or_required,
            'null': error_empty_or_required,
            'required': error_empty_or_required,
            'min_length': error_empty_or_required,
        },
    )
    best_answer = serializers.IntegerField(
        required=False,
        error_messages={
            'invalid': ugettext_lazy("Invalid choice."),
        },
    )
    poll = serializers.IntegerField(
        required=False,
        error_messages={
            'invalid': ugettext_lazy("Invalid choice."),
        },
    )

    def validate_threads(self, data):
        if len(data) > THREADS_LIMIT:
            message = ungettext(
                "No more than %(limit)s thread can be merged at single time.",
                "No more than %(limit)s threads can be merged at single time.",
                POSTS_LIMIT,
            )
            raise ValidationError(message % {'limit': THREADS_LIMIT})
        return data
    
    def get_valid_threads(self, threads_ids):
        user = self.context['user']

        threads_tree_id = trees_map.get_tree_id_for_root(THREADS_ROOT_NAME)
        threads_queryset = Thread.objects.filter(
            id__in=threads_ids,
            category__tree_id=threads_tree_id,
        ).select_related('category').order_by('-id')

        invalid_threads = []
        valid_threads = []
        for thread in threads_queryset:
            add_acl(user, thread)
            if can_see_thread(user, thread):
                valid_threads.append(thread)
                try:
                    allow_merge_thread(user, thread)
                except PermissionDenied as permission_error:
                    invalid_threads.append({
                        'id': thread.id,
                        'status': 403,
                        'detail': permission_error
                    })

        not_found_ids = set(threads_ids) - set([t.id for t in valid_threads])
        for not_found_id in not_found_ids:
            invalid_threads.append({
                'id': not_found_id,
                'status': 404,
                'detail': _(
                    "Requested thread doesn't exist or you don't have permission to see it."
                ),
            })

        if invalid_threads:
            invalid_threads.sort(key=lambda item: item['id'])
            raise ValidationError({'merge': invalid_threads})

        return valid_threads

    def validate(self, data):
        data['threads'] = self.get_valid_threads(data['threads'])

        merge_conflict = MergeConflict(data, data['threads'])
        merge_conflict.is_valid(raise_exception=True)
        data.update(merge_conflict.get_resolution())
        self.merge_conflict = merge_conflict.get_conflicting_fields()
        
        return data
