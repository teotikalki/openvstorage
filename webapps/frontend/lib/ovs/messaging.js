// Copyright 2014 Open vStorage NV
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
/*global define, window */
define([
    'jquery',
    'ovs/shared', 'ovs/api', 'ovs/generic'
], function($, shared, api, generic) {
    "use strict";
    return function() {
        var self = this;

        self.shared        = shared;
        self.subscriberID  = Math.random().toString().substr(3, 10);
        self.lastMessageID = 0;
        self.requestHandle = undefined;
        self.abort         = false;
        self.subscriptions = {};
        self.running       = false;

        self.getSubscriptions = function(type) {
            var callbacks,
                subscription = self.subscriptions[type];
            if (subscription === undefined) {
                callbacks = $.Callbacks();
                subscription = {
                    publish    : callbacks.fire,
                    subscribe  : callbacks.add,
                    unsubscribe: callbacks.remove
                };
                if (type !== undefined) {
                    self.subscriptions[type] = subscription;
                }
            }
            return subscription;
        };
        self.subscribe = function(type, callback) {
            self.getSubscriptions(type).subscribe(callback);
            if (self.running) {
                self.sendSubscriptions();
            }
        };
        self.unsubscribe = function(type, callback) {
            self.getSubscriptions(type).unsubscribe(callback);
        };
        self.broadcast = function(message) {
            var subscription = self.getSubscriptions(message.type);
            subscription.publish.apply(subscription, [message.body]);
        };
        self.getLastMessageID = function() {
            return api.get('messages/' + self.subscriberID + '/last');
        };
        self.start = function() {
            return $.Deferred(function(deferred) {
                self.abort = false;
                self.getLastMessageID()
                    .then(function (messageID) {
                        self.lastMessageID = messageID;
                    })
                    .then(self.sendSubscriptions)
                    .done(function () {
                        self.running = true;
                        self.wait();
                        deferred.resolve();
                    })
                    .fail(function () {
                        deferred.reject();
                        throw "Last message id could not be loaded.";
                    });
            }).promise();
        };
        self.stop = function() {
            self.abort = true;
            generic.xhrAbort(self.requestHandle);
            self.running = false;
        };
        self.sendSubscriptions = function() {
            return api.post('messages/' + self.subscriberID + '/subscribe', { data: generic.keys(self.subscriptions) });
        };
        self.wait = function() {
            generic.xhrAbort(self.requestHandle);
            self.requestHandle = api.get('messages/' + self.subscriberID + '/wait', {
                queryparams: { 'message_id': self.lastMessageID },
                timeout: 1000 * 60 * 1.25
            })
                .done(function(data) {
                    var i, subscriptions = generic.keys(self.subscriptions), resubscribe = false;
                    self.lastMessageID = data.last_message_id;
                    for (i = 0; i < data.messages.length; i += 1) {
                        self.broadcast(data.messages[i]);
                    }
                    for (i = 0; i < subscriptions.length; i += 1) {
                        if ($.inArray(subscriptions[i], data.subscriptions) === -1) {
                            resubscribe = true;
                        }
                    }
                    if (resubscribe) {
                        self.sendSubscriptions()
                        .always(function() {
                            if (!self.abort) {
                                self.wait();
                            }
                        });
                    } else if (!self.abort) {
                        self.wait();
                    }
                })
                .fail(function() {
                    if (!self.abort) {
                        window.setTimeout(function() {
                            self.start().always(shared.tasks.validateTasks);
                        }, 5000);
                    }
                });
        };
    };
});
