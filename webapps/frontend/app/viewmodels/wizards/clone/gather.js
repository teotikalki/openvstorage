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
/*global define */
define([
    'jquery', 'knockout',
    '../../containers/vmachine', './data'
], function($, ko, VMachine, data) {
    "use strict";
    return function() {
        var self = this;

        // Variables
        self.data = data;

        // Computed
        self.canContinue = ko.computed(function() {
            var valid = true, reasons = [], fields = [];
            if (self.data.vm() === undefined) {
                valid = false;
                fields.push('vm');
                reasons.push($.t('ovs:wizards.clone.gather.nomachine'));
            }
            if (!self.data.name()) {
                valid = false;
                fields.push('name');
                reasons.push($.t('ovs:wizards.clone.gather.noname'));
            }
            if (self.data.vm().snapshots().length === 0) {
                valid = false;
                fields.push('snapshots');
                reasons.push($.t('ovs:wizards.clone.gather.nosnapshots'));
            }
            return { value: valid, reasons: reasons, fields: fields };
        });

        // Durandal
        self.activate = function() {
            if (self.data.vm() === undefined || self.data.vm().guid() !== self.data.machineGuid()) {
                self.data.vm(new VMachine(self.data.machineGuid()));
                self.data.vm()
                    .load()
                    .done(function() {
                        self.data.name(self.data.vm().name() + '-clone');
                    });
            }
        };
    };
});
